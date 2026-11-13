"""Celery worker entrypoint and background task definitions."""
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4
from zoneinfo import ZoneInfo

from flask import current_app

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from webapp import create_app
from webapp.extensions import celery as _celery, db
from webapp.integrations.firstcycling.api import FirstCyclingClient
from webapp.integrations.pcs.profile import parse_pcs_profile
from webapp.integrations.pcs.race import RACE_ENDPOINT as PCS_RACE_ENDPOINT, parse_race_url, fetch_race as fetch_pcs_race, store_race as store_pcs_race
from webapp.integrations.pcs.slug import rider_profile_url, rider_slug_from_profile_url, valid_rider_slug
from webapp.integrations.rider_checks import (
    PROFILE_ENDPOINT, apply_rider_check,
    RIDER_CHECK_JOBS, enqueue_rider_check,
)
from webapp.models import ExternalCallTask, ExternalCallTaskGroup, ScheduledJob, User
from webapp.models.main.namespace import Namespace

SYNC_TYPE_RACE = 'fc_race_calendar'
SYNC_TYPE_TEAMS = 'fc_teams'
SYNC_TYPE_RIDERS = 'fc_riders'
SYNC_TYPE_RACE_RESULTS = 'fc_race_results'
RACE_RESULTS_ENDPOINT = 'get_race_results'
logger = logging.getLogger(__name__)
PCS_MAX_ATTEMPTS = 2


class RetryableFetch(Exception):
    def __init__(self, message, delay=60, http_status=None):
        super().__init__(message)
        self.delay = delay
        self.http_status = http_status


class ProfileProcessingError(ValueError):
    def __init__(self, message, http_status):
        super().__init__(message)
        self.http_status = http_status


def retry_after(value):
    try:
        return max(float(value), 0)
    except (TypeError, ValueError):
        try:
            return max((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds(), 0)
        except (TypeError, ValueError, OverflowError):
            return 60


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_request(provider, endpoint, params):
    if provider == 'pcs' and endpoint == PCS_RACE_ENDPOINT:
        if (not isinstance(params, dict) or type(params.get('season_year')) is not int
                or not 1 <= params['season_year'] <= 9999 or params.get('gender') not in {'Men', 'Women'}):
            raise ValueError('A valid PCS race season and gender are required')
        parsed = parse_race_url(params.get('race_url'), params['season_year'])
        if parsed['race_slug'] != params.get('race_slug') or parsed['race_url'] != params['race_url']:
            raise ValueError('A canonical PCS race URL is required')
        return
    if provider == 'firstcycling' and endpoint == RACE_RESULTS_ENDPOINT:
        if not isinstance(params, dict):
            raise ValueError('Request parameters must be an object')
        for name in ('race_id', 'season_year'):
            value = params.get(name)
            if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
                raise ValueError(f'A positive {name} is required')
        if int(params['season_year']) > 9999 or params.get('gender') not in {'Men', 'Women'}:
            raise ValueError('A valid race season and gender are required')
        return
    if endpoint != PROFILE_ENDPOINT or provider not in {'pcs', 'firstcycling'}:
        raise ValueError('Unsupported provider/endpoint')
    if not isinstance(params, dict):
        raise ValueError('Request parameters must be an object')
    if provider == 'pcs':
        slug = params.get('rider_slug', '')
        if not valid_rider_slug(slug):
            raise ValueError('A canonical PCS rider_slug is required')
    else:
        rider_id = params.get('rider_id')
        if isinstance(rider_id, bool) or not str(rider_id).isdigit() or int(rider_id) <= 0:
            raise ValueError('A positive FirstCycling rider_id is required')


def fetch_pcs_html(target):
    """Fetch one validated PCS URL without redirects; the queue owns retries."""
    request = Request(target, headers={'User-Agent': 'WorldDB/1.0', 'Accept': 'text/html'})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=30) as response:
            html = response.read(5_000_001)
            if len(html) > 5_000_000:
                raise ValueError('PCS page exceeds size limit')
            return html
    except HTTPError as exc:
        if exc.code in {403, 429} or exc.code >= 500:
            raise RetryableFetch('PCS cooldown required', max(60, retry_after(exc.headers.get('Retry-After'))), exc.code) from exc
        raise ValueError(f'PCS HTTP {exc.code}') from exc
    except (URLError, TimeoutError) as exc:
        raise RetryableFetch('PCS connection failure') from exc


def fetch_profile(provider, target):
    if provider == 'firstcycling':
        from webapp.integrations.firstcycling.api import FirstCyclingClient
        client = FirstCyclingClient()
        # Queue owns attempts. FC has no queue delay or PCS rate policy.
        client.request_delay_seconds = 0
        client.retry_attempts = 0
        try:
            payload, _ = client.get_rider(int(target['rider_id']))
            return payload
        except Exception as exc:
            cause = exc.__cause__
            if isinstance(cause, HTTPError) and (cause.code == 429 or cause.code >= 500):
                raise RetryableFetch('FirstCycling temporarily unavailable', retry_after(cause.headers.get('Retry-After')), cause.code) from exc
            if isinstance(cause, URLError):
                raise RetryableFetch('FirstCycling connection failure') from exc
            raise

    slug = rider_slug_from_profile_url(target)
    if slug is None:
        raise ValueError('A canonical PCS rider profile URL is required')
    request = Request(target, headers={'User-Agent': 'WorldDB/1.0', 'Accept': 'text/html'})
    response_status = None
    try:
        with build_opener(_NoRedirect()).open(request, timeout=30) as response:
            response_status = getattr(response, 'status', 200)
            html = response.read(5_000_001)
            if len(html) > 5_000_000:
                raise ValueError('PCS profile exceeds size limit')
        parsed = parse_pcs_profile(html)
        canonical_slug = parsed['profile'].get('canonical_slug')
        if canonical_slug and canonical_slug != slug:
            raise ValueError('PCS returned a different rider profile')
        if parsed['status'] not in {'results_parsed', 'results_table_empty'}:
            raise ValueError(f"PCS profile could not be parsed: {parsed['status']}")
        return parsed
    except HTTPError as exc:
        if response_status == 200:
            raise ProfileProcessingError(str(exc), response_status) from exc
        if exc.code in {403, 429} or exc.code >= 500:
            raise RetryableFetch('PCS cooldown required', max(60, retry_after(exc.headers.get('Retry-After'))), exc.code) from exc
        raise ValueError(f'PCS profile HTTP {exc.code}') from exc
    except (URLError, TimeoutError) as exc:
        if response_status == 200:
            raise ProfileProcessingError(str(exc), response_status) from exc
        raise RetryableFetch('PCS connection failure') from exc
    except Exception as exc:
        raise ProfileProcessingError(str(exc), response_status) from exc


# Stable PostgreSQL advisory lock key, shared by every queue worker.
PCS_LOCK = 812734902


def enqueue_group(requests, *, namespace_id=None, executed_by_id=None, message=None,
                  origin='manual', priority=None, provider=None, workflow_type=None, scheduled_job_id=None,
                  draft=False, season_year=None, gender=None):
    """Validate all requests before inserting; caller commits before dispatch."""
    if origin not in {'manual', 'scheduled'}:
        raise ValueError('Unsupported task group origin')
    priority = priority if priority is not None else ('high' if origin == 'manual' else 'low')
    if priority not in {'high', 'low'}:
        raise ValueError('Unsupported task group priority')
    requests = list(requests)
    if not requests and not draft:
        raise ValueError('A task group requires at least one request')
    for request in requests:
        validate_request(request['provider'], request['endpoint'], request['params'])
    group = ExternalCallTaskGroup(status='draft' if draft else 'queued', origin=origin, priority=priority,
                                  namespace_id=namespace_id, executed_by_id=executed_by_id,
                                  message=message, requested_count=len(requests),
                                  provider=provider, workflow_type=workflow_type, scheduled_job_id=scheduled_job_id,
                                  season_year=season_year, gender=gender)
    db.session.add(group)
    db.session.flush()
    _add_group_requests(group, requests)
    return group


def _add_group_requests(group, requests):
    group.sync_types = sorted({f"{request['provider']}.{request['endpoint']}" for request in requests})
    for request in requests:
        is_pcs = request['provider'] == 'pcs'
        is_pcs_profile = is_pcs and request['endpoint'] == PROFILE_ENDPOINT
        # Keep the validated slug available to workers that predate URL-backed tasks.
        params = {'rider_slug': request['params']['rider_slug']} if is_pcs_profile else request['params']
        db.session.add(ExternalCallTask(
            task_group_id=group.id, provider=request['provider'], endpoint=request['endpoint'],
            request_url=(rider_profile_url(params['rider_slug']) if is_pcs_profile else params['race_url'] if is_pcs else None),
            request_params_json=json.dumps(params), status='draft' if group.status == 'draft' else 'queued',
            related_to_id=request.get('related_to_id'),
            related_to_table=request.get('related_to_table'),
            attempt=0, max_attempts=PCS_MAX_ATTEMPTS if is_pcs else 3,
        ))
    db.session.flush()


def replace_draft_requests(group, requests):
    """Replace a locked draft's membership after validating the entire request."""
    if group.status != 'draft':
        raise ValueError('Only draft groups can be edited.')
    for request in requests:
        validate_request(request['provider'], request['endpoint'], request['params'])
    ExternalCallTask.query.filter_by(task_group_id=group.id).delete()
    _add_group_requests(group, requests)
    group.requested_count = len(requests)


def cancel_external_group(group_id):
    """Stop pending requests while allowing an in-flight request to finish."""
    group = db.session.scalar(select(ExternalCallTaskGroup).where(
        ExternalCallTaskGroup.id == group_id,
    ).with_for_update().execution_options(populate_existing=True))
    if group is None or group.status not in {'draft', 'queued', 'running'}:
        raise ValueError('Only draft, queued, or running groups can be cancelled.')
    group.status = 'cancelled'
    group.is_worker_queued = False
    group.worker_message_id = None
    ExternalCallTask.query.filter(
        ExternalCallTask.task_group_id == group_id,
        ExternalCallTask.status.in_(['draft', 'queued', 'retry_wait']),
    ).update(dict(status='cancelled', outcome='cancelled', finished_at=datetime.utcnow()),
             synchronize_session='fetch')


def _lock_task_group(task_id):
    # Claims, completions, and cancellation lock the group before its tasks.
    return db.session.scalar(select(ExternalCallTaskGroup).where(
        ExternalCallTaskGroup.id == select(ExternalCallTask.task_group_id).where(
            ExternalCallTask.id == task_id,
        ).scalar_subquery(),
    ).with_for_update().execution_options(populate_existing=True))


def _refresh_group(group_id):
    if group_id is None:
        return
    group = db.session.execute(select(ExternalCallTaskGroup).where(
        ExternalCallTaskGroup.id == group_id).with_for_update()).scalar_one()
    # Only queue tasks affect queue group aggregates; FC audit rows do not.
    states = db.session.execute(select(ExternalCallTask.status).where(
        ExternalCallTask.task_group_id == group_id,
        ExternalCallTask.status != 'historical')).scalars().all()
    group.synced_count = states.count('success')
    group.failed_count = states.count('failed')
    if group.status in {'draft', 'cancelled'}:
        return
    if 'running' in states:
        group.status = 'running'
    elif any(state in {'queued', 'retry_wait'} for state in states):
        group.status = 'queued'
    elif group.failed_count:
        group.status = 'partial' if group.synced_count else 'failed'
    else:
        group.status = 'success'
    if group.status in {'success', 'partial', 'failed'}:
        group.worker_message_id = None
        group.is_worker_queued = False


def fetch_race_results(params, group_id):
    """Use the existing FC importer for one validated edition, with group audit logs."""
    from webapp.models import FirstCyclingRace

    race = FirstCyclingRace.query.filter_by(
        race_id=int(params['race_id']), season_year=int(params['season_year']),
        gender=params['gender'],
    ).first()
    if race is None or (race.start_date and race.start_date > datetime.utcnow().date()):
        raise ValueError('The selected race edition is unavailable for result fetching.')
    client = FirstCyclingClient(external_sync_log_id=group_id)
    client.retry_attempts = 0
    try:
        return client.sync_race_results(race_id=race.race_id, season_year=race.season_year, commit=True)
    except Exception as exc:
        db.session.rollback()
        cause = exc.__cause__
        if isinstance(cause, HTTPError) and (cause.code == 429 or cause.code >= 500):
            raise RetryableFetch('FirstCycling temporarily unavailable', retry_after(cause.headers.get('Retry-After')), cause.code) from exc
        if isinstance(cause, URLError):
            raise RetryableFetch('FirstCycling connection failure') from exc
        raise


def execute_task(task_id):
    """Claim exactly once, commit before I/O, then store results and group state."""
    now = datetime.utcnow()
    group = _lock_task_group(task_id)
    if group is not None and group.status in {'draft', 'cancelled'}:
        db.session.rollback()
        return False
    task = db.session.execute(select(ExternalCallTask).where(
        ExternalCallTask.id == task_id).with_for_update(skip_locked=True)).scalar_one_or_none()
    if task is None or task.status not in {'queued', 'retry_wait'} or task.available_at > now:
        db.session.rollback()
        return False
    if task.provider == 'pcs':
        task.max_attempts = min(task.max_attempts, PCS_MAX_ATTEMPTS)
        if task.attempt >= task.max_attempts or (task.attempt > 0 and task.http_status == 200):
            task.status = task.outcome = 'failed'
            task.finished_at = now
            _refresh_group(task.task_group_id)
            db.session.commit()
            return True
        # Non-blocking lock avoids deadlocks with workers holding other task rows.
        if not db.session.execute(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key': PCS_LOCK}).scalar():
            db.session.rollback()
            return False
        running = db.session.execute(select(ExternalCallTask.id).where(
            ExternalCallTask.provider == 'pcs', ExternalCallTask.status == 'running')).first()
        last_start, cooldown = db.session.execute(select(
            func.max(ExternalCallTask.started_at), func.max(ExternalCallTask.available_at)
        ).where(ExternalCallTask.provider == 'pcs', ExternalCallTask.started_at.isnot(None))).one()
        delay = max(1, float(current_app.config.get('PCS_REQUEST_DELAY_SECONDS', 5)))
        if running or (last_start and now < last_start + timedelta(seconds=delay)) or (cooldown and cooldown > now):
            db.session.rollback()
            return False
    task.status = 'running'
    task.started_at = now
    task.attempt += 1
    provider, endpoint = task.provider, task.endpoint
    request_url, params_json = task.request_url, task.request_params_json
    attempt, max_attempts = task.attempt, task.max_attempts
    _refresh_group(task.task_group_id)
    db.session.commit()
    params, payload, result = {}, None, None
    error, status, available_at, http_status = None, 'success', now, None
    try:
        if provider == 'pcs' and endpoint == PROFILE_ENDPOINT and request_url is not None:
            validate_request(provider, endpoint, {'rider_slug': rider_slug_from_profile_url(request_url)})
        else:
            params = json.loads(params_json or '{}')
            validate_request(provider, endpoint, params)
            if provider == 'pcs' and endpoint == PROFILE_ENDPOINT:
                # Tasks queued before PCS URLs were stored still carry their slug in JSON.
                request_url = rider_profile_url(params['rider_slug'])
        if provider == 'pcs' and endpoint == PCS_RACE_ENDPOINT:
            payload = fetch_pcs_race(params)
        elif provider == 'firstcycling' and endpoint == RACE_RESULTS_ENDPOINT:
            payload = fetch_race_results(params, task.task_group_id)
        else:
            payload = fetch_profile(provider, request_url if provider == 'pcs' else params)
        if provider == 'pcs':
            http_status = 200
        result = json.dumps(payload, default=str)
    except RetryableFetch as exc:
        status = 'retry_wait' if attempt < max_attempts else 'failed'
        available_at = datetime.utcnow() + timedelta(seconds=max(exc.delay, 2 ** attempt))
        error, http_status = str(exc), exc.http_status
    except Exception as exc:
        status, error = 'failed', str(exc)
        if isinstance(exc, ProfileProcessingError):
            http_status = exc.http_status
    # Serialize duplicate completion and group aggregate updates.
    group = _lock_task_group(task_id)
    task = db.session.execute(select(ExternalCallTask).where(
        ExternalCallTask.id == task_id).with_for_update()).scalar_one()
    if task.status != 'running' or task.attempt != attempt:
        db.session.rollback()
        return False
    if provider == 'pcs' and task.request_url is None and request_url is not None:
        task.request_url = request_url
    checked_at = datetime.utcnow()
    if group is not None and group.status == 'cancelled' and status == 'retry_wait':
        status = 'cancelled'
    if provider == 'pcs' and endpoint == PCS_RACE_ENDPOINT and status == 'success':
        try:
            with db.session.begin_nested():
                result = json.dumps(store_pcs_race(params, payload, namespace_id=group.namespace_id if group else None))
        except Exception as exc:
            status, error = 'failed', str(exc)
    if endpoint not in {RACE_RESULTS_ENDPOINT, PCS_RACE_ENDPOINT} and status not in {'retry_wait', 'cancelled'} and isinstance(params, dict):
        try:
            with db.session.begin_nested():
                apply_rider_check(task, params, payload, status, checked_at)
        except Exception as exc:
            logger.warning('Rider check post-processing failed for task %s (%s)', task.id, type(exc).__name__)
            error = f'Could not apply rider check results ({type(exc).__name__})'
            status = 'retry_wait' if provider != 'pcs' and attempt < max_attempts else 'failed'
            if status == 'retry_wait':
                available_at = checked_at + timedelta(seconds=2 ** attempt)
    if group is not None and group.status == 'cancelled' and status == 'retry_wait':
        status = 'cancelled'
    task.status, task.result_json, task.error_message = status, result, error
    task.available_at, task.http_status = available_at, http_status
    task.outcome = status
    task.finished_at = checked_at if status != 'retry_wait' else None
    _refresh_group(task.task_group_id)
    db.session.commit()
    return True


@_celery.task(name='webapp.tasks.run_external_call')
def run_external_call(task_id):
    # Old per-request messages must not bypass group ordering after upgrading.
    logger.warning('Ignoring legacy external-call delivery: task_id=%s', task_id)
    return False


def submit_external_group(group_id):
    """Publish one committed group, serializing the shared low-queue budget.

    The row lock stays held through publication so a fast consumer cannot claim
    the group before its submission metadata commits. Broker publication and a
    database commit are not atomic; message tokens and execution locks make a
    repeated delivery harmless if a publisher loses its connection.
    """
    db.session.execute(text('SELECT pg_advisory_xact_lock(812734904)'))
    group = db.session.scalar(select(ExternalCallTaskGroup).where(
        ExternalCallTaskGroup.id == group_id,
    ).with_for_update())
    if group is None or group.status != 'queued' or group.is_worker_queued or group.worker_message_id:
        db.session.rollback()
        return False
    if not db.session.scalar(select(ExternalCallTask.id).where(
        ExternalCallTask.task_group_id == group.id,
        ExternalCallTask.status.in_(['queued', 'retry_wait']),
    ).limit(1)):
        db.session.rollback()
        return False
    if group.priority == 'low':
        waiting = db.session.scalar(select(func.count()).select_from(ExternalCallTaskGroup).where(
            ExternalCallTaskGroup.priority == 'low',
            ExternalCallTaskGroup.status == 'queued',
            ExternalCallTaskGroup.is_worker_queued.is_(True),
        ))
        if waiting >= current_app.config['PCS_RIDER_CHECK_SCHEDULER_TASKGROUP_CELERY_MAX_QUEUED']:
            db.session.rollback()
            return False
    token = str(uuid4())
    group.is_worker_queued = True
    group.worker_message_id = token
    group.worker_queued_at = datetime.utcnow()
    try:
        run_external_group.apply_async(
            args=[group.id, token], task_id=token, queue=f'external.{group.priority}',
            retry=False,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return True


def create_manual_external_group(requests, *, namespace_id=None, executed_by_id=None, message=None,
                                 provider=None, workflow_type=None, season_year=None, gender=None):
    """Create/commit high-priority work and notify Celery immediately.

    Call from an authorized application action after resolving selected riders.
    This commits the caller's transaction; do not include unrelated pending work.
    A broker failure leaves a queued database group for the hourly dispatcher.
    """
    group = enqueue_group(requests, namespace_id=namespace_id,
                          executed_by_id=executed_by_id, message=message, origin='manual',
                          provider=provider, workflow_type=workflow_type, season_year=season_year, gender=gender)
    group_id = group.id
    db.session.commit()
    try:
        submit_external_group(group_id)
    except Exception:
        logger.exception('Could not submit manual external group: group_id=%s', group_id)
    return db.session.get(ExternalCallTaskGroup, group_id)


def enqueue_scheduled_pcs_groups(requests, *, namespace_id=None):
    """Batch selected PCS riders into unlimited low-priority database groups.

    One canonical PCS profile request represents one rider. Selection SQL and
    next-check-date fields are future work. Caller commits all generated groups
    before dispatch_queued_external_groups; no Celery publication happens here.
    """
    requests = list(requests)
    slugs = set()
    for request in requests:
        validate_request(request['provider'], request['endpoint'], request['params'])
        if request['provider'] != 'pcs':
            raise ValueError('Scheduled PCS groups accept only PCS rider requests')
        slug = request['params']['rider_slug']
        if slug in slugs:
            raise ValueError('Scheduled rider selection must contain distinct PCS riders')
        slugs.add(slug)
    size = current_app.config['PCS_RIDER_CHECK_SCHEDULER_TASKGROUP_MAX_SIZE']
    return [enqueue_group(requests[offset:offset + size], namespace_id=namespace_id,
                          origin='scheduled') for offset in range(0, len(requests), size)]


def dispatch_queued_external_groups():
    """Fill available low-priority slots; high-priority recovery is uncapped."""
    ids = db.session.scalars(select(ExternalCallTaskGroup.id).where(
        ExternalCallTaskGroup.status == 'queued',
        ExternalCallTaskGroup.is_worker_queued.is_(False),
        ExternalCallTaskGroup.worker_message_id.is_(None),
    ).order_by(ExternalCallTaskGroup.timestamp, ExternalCallTaskGroup.id)).all()
    db.session.rollback()
    submitted = []
    for group_id in ids:
        if submit_external_group(group_id):
            submitted.append(group_id)
    return submitted


def _next_group_task(group_id):
    """Return runnable work and the earliest retry/spacing wait in this group."""
    now = datetime.utcnow()
    running, last_start, cooldown = db.session.execute(select(
        func.count().filter(ExternalCallTask.status == 'running'),
        func.max(ExternalCallTask.started_at),
        func.max(ExternalCallTask.available_at).filter(ExternalCallTask.started_at.isnot(None)),
    ).where(ExternalCallTask.provider == 'pcs')).one()
    delay = max(1, float(current_app.config.get('PCS_REQUEST_DELAY_SECONDS', 5)))
    pcs_ready = not (running or (last_start and now < last_start + timedelta(seconds=delay))
                     or (cooldown and cooldown > now))
    pcs_after = max(filter(None, [
        now, last_start + timedelta(seconds=delay) if last_start else None, cooldown,
    ]))
    pending = db.session.scalars(select(ExternalCallTask).where(
        ExternalCallTask.task_group_id == group_id,
        ExternalCallTask.status.in_(['queued', 'retry_wait']),
    ).order_by(ExternalCallTask.id)).all()
    waits = []
    task_id = None
    for task in pending:
        ready_at = task.available_at
        if task.provider == 'pcs' and not pcs_ready:
            ready_at = max(ready_at, pcs_after, now + timedelta(seconds=1) if running else now)
        if ready_at <= now:
            task_id = task.id
            break
        waits.append((ready_at - now).total_seconds())
    db.session.rollback()
    return task_id, max(0.1, min(waits)) if waits else None


@_celery.task(bind=True, name='webapp.tasks.run_external_group', acks_late=True,
              reject_on_worker_lost=True, max_retries=None)
def run_external_group(self, group_id, submission_id):
    """Finish one group, retaining ownership through provider/retry waits."""
    with db.engines['main'].connect() as connection:
        acquired = connection.scalar(text('SELECT pg_try_advisory_lock(812734903)'))
        connection.commit()
        if not acquired:
            raise self.retry(countdown=5)
        processed = 0
        try:
            group = db.session.scalar(select(ExternalCallTaskGroup).where(
                ExternalCallTaskGroup.id == group_id,
            ).with_for_update())
            if (group is None or not submission_id or group.worker_message_id != submission_id
                    or group.status not in {'queued', 'running'}):
                db.session.rollback()
                return 0
            group.is_worker_queued = False
            # Owning the execution lock proves any shared-queue running request
            # was interrupted, including one from a different redelivered group.
            # Retain its failure for review rather than silently fetching it twice.
            interrupted = db.session.scalars(select(ExternalCallTask).where(
                ExternalCallTask.status == 'running',
            ).with_for_update()).all()
            for task in interrupted:
                task.status = 'failed'
                task.error_message = 'Worker interrupted; review before requeueing'
                task.finished_at = datetime.utcnow()
            for interrupted_group_id in sorted({task.task_group_id for task in interrupted
                                                if task.task_group_id not in {None, group_id}}):
                _refresh_group(interrupted_group_id)
            db.session.commit()
            while True:
                task_id, wait = _next_group_task(group_id)
                if task_id is None:
                    if wait is not None:
                        time.sleep(min(wait, 30))
                        continue
                    _refresh_group(group_id)
                    db.session.commit()
                    break
                if execute_task(task_id):
                    processed += 1
                else:
                    time.sleep(0.1)
            return processed
        except Exception as exc:
            db.session.rollback()
            # Keep the same group/message identity on infrastructure failures.
            # Normal provider retries stay inside the loop above.
            raise self.retry(exc=exc, countdown=30)
        finally:
            db.session.rollback()
            connection.execute(text('SELECT pg_advisory_unlock(812734903)'))
            connection.commit()


@_celery.task(name='webapp.tasks.poll_external_calls', acks_late=True)
def poll_external_calls():
    """Dispatch committed external call groups."""
    return dispatch_queued_external_groups()


def _update_sync_log(log_id, **updates):
    log = ExternalCallTaskGroup.query.get(log_id)
    if log is None:
        return None

    for key, value in updates.items():
        setattr(log, key, value)

    db.session.commit()
    return log


@_celery.task(name='webapp.tasks.run_external_sync')
def run_external_sync_task(log_id, sync_type, season_year, gender, api_path=None, start_date=None, end_date=None, limit=None):
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
                limit=limit,
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
                limit=limit,
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

        if limit is not None and sync_type in (SYNC_TYPE_TEAMS, SYNC_TYPE_RIDERS):
            summary['limit'] = limit
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


FC_RESULTS_SCHEDULE_LOCK = 812734905


@_celery.task(name='webapp.tasks.run_rider_check_now')
def run_rider_check_now(job, namespace_id, executed_by_id, settings, job_id=None):
    """Queue a one-off rider check for the selected namespace."""
    if job not in RIDER_CHECK_JOBS:
        return []
    if job_id is not None:
        saved_job = db.session.get(ScheduledJob, job_id)
        if saved_job is None or (saved_job.job_type, saved_job.namespace_id) != (job, namespace_id):
            return []
    namespace = db.session.get(Namespace, namespace_id)
    if (namespace is None or not namespace.is_active or namespace.is_template
            or db.session.get(User, executed_by_id) is None):
        return []

    try:
        now = datetime.now(ZoneInfo(current_app.config['WORKER_TIMEZONE']))
        group_ids = enqueue_rider_check(job, now, enqueue_group, namespace, settings,
                                        job_id=job_id, executed_by_id=executed_by_id)
    except Exception:
        db.session.rollback()
        logger.exception('Could not select riders for %s in namespace %s', job, namespace_id)
        group_ids = []
        status, message = 'failed', 'Could not select riders for this check.'
    else:
        status, message = 'success', 'No eligible riders matched these settings.'
    if not group_ids:
        db.session.add(ExternalCallTaskGroup(
            namespace_id=namespace_id, executed_by_id=executed_by_id, workflow_type=job,
            scheduled_job_id=job_id, status=status, requested_count=0, message=f'{RIDER_CHECK_JOBS[job]}: {message}',
        ))
    db.session.commit()

    for group_id in group_ids:
        try:
            submit_external_group(group_id)
        except Exception:
            logger.exception('Could not submit rider check group %s', group_id)
            break  # Unsubmitted groups are recovered by the queue poller.
    return group_ids


def _queue_due_scheduled_job(job_id, now):
    job = db.session.get(ScheduledJob, job_id, with_for_update=True)
    today = now.date()
    if (job is None or not job.enabled or now.time() < job.run_at
            or (job.last_run_on is not None and job.last_run_on >= today)):
        db.session.rollback()
        return

    if job.job_type == 'race_results':
        settings = job.params
        start = today - timedelta(days=settings['start_days_ago'])
        end = today - timedelta(days=settings['end_days_ago'])
        db.session.add(ExternalCallTaskGroup(
            scheduled_job_id=job.id, provider='firstcycling', workflow_type=SYNC_TYPE_RACE_RESULTS,
            origin='scheduled', priority='low', season_year=settings['year'], gender=settings['gender'],
            status='queued', message=f'Queued scheduled race results sync ({start} to {end})',
            details_json=json.dumps({'start_date': start.isoformat(), 'end_date': end.isoformat()}),
        ))
    else:
        try:
            with db.session.begin_nested():
                enqueue_rider_check(job.job_type, now, enqueue_group, job.namespace, job.params, job_id=job.id)
        except ValueError as exc:
            db.session.add(ExternalCallTaskGroup(
                scheduled_job_id=job.id, namespace_id=job.namespace_id, workflow_type=job.job_type,
                origin='scheduled', priority='low', status='failed', failed_count=1, message=str(exc),
            ))
    job.last_run_on = today
    db.session.commit()


@_celery.task(name='webapp.tasks.poll_external_sync_schedules', acks_late=True, reject_on_worker_lost=True)
def poll_external_sync_schedules():
    """Claim each due job once per local day and process queued race result work."""
    with db.engines['main'].connect() as connection:
        acquired = connection.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': FC_RESULTS_SCHEDULE_LOCK})
        connection.commit()
        if not acquired:
            return None
        try:
            now = datetime.now(ZoneInfo(current_app.config['WORKER_TIMEZONE']))
            job_ids = db.session.scalars(select(ScheduledJob.id).where(
                ScheduledJob.enabled.is_(True), ScheduledJob.run_at <= now.time(),
                (ScheduledJob.last_run_on.is_(None)) | (ScheduledJob.last_run_on < now.date()),
            ).order_by(ScheduledJob.id)).all()
            for job_id in job_ids:
                try:
                    _queue_due_scheduled_job(job_id, now)
                except Exception:
                    db.session.rollback()
                    logger.exception('Could not enqueue scheduled job %s', job_id)

            groups = ExternalCallTaskGroup.query.filter_by(
                provider='firstcycling', workflow_type=SYNC_TYPE_RACE_RESULTS, origin='scheduled', namespace_id=None,
            )
            groups.filter_by(status='running').update({
                'status': 'failed', 'message': 'Worker interrupted during the scheduled race results sync.',
                'failed_count': 1,
            })
            queued = groups.filter_by(status='queued').order_by(ExternalCallTaskGroup.id).all()
            db.session.commit()
            last_group_id = None
            for group in queued:
                last_group_id = group.id
                try:
                    dates = json.loads(group.details_json)
                    run_external_sync_task.run(
                        log_id=group.id, sync_type=group.workflow_type, season_year=group.season_year,
                        gender=group.gender, start_date=dates['start_date'], end_date=dates['end_date'],
                    )
                except Exception:
                    db.session.rollback()
                    logger.exception('Scheduled race results group %s failed', last_group_id)
            return last_group_id
        finally:
            db.session.rollback()
            connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': FC_RESULTS_SCHEDULE_LOCK})
            connection.commit()


def __getattr__(name):
    """Initialize Flask only when the Celery CLI loads the worker app."""
    if name == 'celery':
        create_app()
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
                        candidate_count, due_rows = due_activity_riders(
                            planning_connection, now.date(), limit=25,
                            source_mode=source_mode, include_upcoming=True,
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
    if name == 'celery':
        create_app()
        globals()['celery'] = _celery
        return _celery
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
