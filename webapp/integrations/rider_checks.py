"""Select namespace riders for the external request queue and apply fetch results."""
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from types import SimpleNamespace

from sqlalchemy import create_engine, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert

from webapp.extensions import db
from webapp.integrations.cdb.rider_state import pcs_dob_identity_status
from webapp.integrations.firstcycling.api import FirstCyclingClient
from webapp.integrations.pcs.slug import (
    PCS_SLUG_PATTERN, request_rider_slug, rider_profile_url, rider_slug_from_profile_url,
)
from webapp.models import ExternalCallTask, ExternalCallTaskGroup, PcsRider, PcsRiderResult
from webapp.models.main.firstcycling import FirstCyclingRace, FirstCyclingRaceResult, FirstCyclingRaceStage
from webapp.models.ns.cdb import Rider


IDENTITY_CHECK = 'rider_identity_check'
ACTIVITY_CHECK = 'rider_activity_check'
RIDER_CHECK_JOBS = {IDENTITY_CHECK: 'Rider Identity Check', ACTIVITY_CHECK: 'Rider Activity Check'}
PCS_RIDER_SYNC = 'pcs.get_rider_profile'
FC_RIDER_SYNC = 'firstcycling.get_rider_profile'
PROFILE_ENDPOINT = 'get_rider_profile'
ACTIVE_TASKS = ('queued', 'retry_wait', 'running')
RIDER = Rider.__table__


def _pcs_request_keys(slug):
    # Keep matching historical tasks that stored the slug before URLs were populated.
    return rider_profile_url(slug), json.dumps({'rider_slug': slug})


@contextmanager
def rider_database(namespace):
    """Open a verified namespace connection, never the shared database."""
    if namespace is None or not namespace.is_active or namespace.is_template:
        raise ValueError('Rider namespace is unavailable')
    engine = create_engine(namespace.build_connection_string())
    try:
        if (engine.url.database != namespace.database_name
                or engine.url.database == db.engines['main'].url.database):
            raise ValueError('Rider database does not match its namespace')
        with engine.begin() as conn:
            if conn.scalar(text('SELECT current_database()')) != namespace.database_name:
                raise ValueError('Rider database does not match its namespace')
            yield conn
    finally:
        engine.dispose()


def _recent_fc_riders(ids, today, days):
    if not days:
        return set()
    return set(db.session.scalars(select(FirstCyclingRaceResult.rider_id).join(
        FirstCyclingRace, FirstCyclingRace.id == FirstCyclingRaceResult.race_id,
    ).outerjoin(
        FirstCyclingRaceStage, FirstCyclingRaceStage.id == FirstCyclingRaceResult.stage_id,
    ).where(
        FirstCyclingRaceResult.rider_id.in_(ids),
        func.coalesce(FirstCyclingRaceStage.stage_date, FirstCyclingRace.start_date,
                      FirstCyclingRace.end_date) <= today,
        func.coalesce(FirstCyclingRaceStage.stage_date, FirstCyclingRace.end_date,
                      FirstCyclingRace.start_date) >= today - timedelta(days=days),
    ).distinct()))


def _scheduled_rider_query():
    return select(RIDER.c.id, RIDER.c.first_name, RIDER.c.last_name, RIDER.c.first_last_name,
                  RIDER.c.pcs_slug, RIDER.c.first_cycling_id).where(
        RIDER.c.is_deleted.is_(False),
        or_(RIDER.c.pcs_slug.op('~')(f'^{PCS_SLUG_PATTERN}$'),
            RIDER.c.pcs_slug.is_(None), RIDER.c.pcs_slug == ''),
        or_(RIDER.c.pcs_slug.is_(None), func.length(RIDER.c.pcs_slug) <= 255),
        RIDER.c.first_cycling_id > 0,
    )


def scheduled_rider_choices(namespace, search='', selected_id=None):
    """Find linked riders in the selected namespace, including the saved selection."""
    with rider_database(namespace) as conn:
        query = _scheduled_rider_query()
        if search:
            query = query.where(or_(
                RIDER.c.first_last_name.icontains(search, autoescape=True),
                RIDER.c.pcs_slug.icontains(search, autoescape=True),
            ))
        rows = list(conn.execute(query.order_by(RIDER.c.first_last_name, RIDER.c.id).limit(100)))
        if selected_id and all(row.id != selected_id for row in rows):
            selected = conn.execute(_scheduled_rider_query().where(RIDER.c.id == selected_id)).one_or_none()
            if selected:
                rows.insert(0, selected)
        return rows


def scheduled_rider(namespace, rider_id):
    with rider_database(namespace) as conn:
        return conn.execute(_scheduled_rider_query().where(RIDER.c.id == rider_id)).one_or_none()


def _identity_candidates(namespace):
    """Read the first eligible PCS riders without requiring FirstCycling links."""
    with rider_database(namespace) as conn:
        query = select(RIDER.c.id, RIDER.c.pcs_slug, RIDER.c.first_name, RIDER.c.last_name).where(
            RIDER.c.is_deleted.is_(False),
            RIDER.c.pcs_identity_status == 'unknown',
            RIDER.c.pcs_identity_verified_override.is_(False),
            or_(RIDER.c.pcs_slug.op('~')(f'^{PCS_SLUG_PATTERN}$'),
                RIDER.c.pcs_slug.is_(None), RIDER.c.pcs_slug == ''),
            or_(RIDER.c.pcs_slug.is_(None), func.length(RIDER.c.pcs_slug) <= 255),
        ).order_by(RIDER.c.id)
        last_id = 0
        while True:
            page = conn.execute(query.where(RIDER.c.id > last_id).limit(500)).all()
            if not page:
                break
            last_id = page[-1].id
            yield [SimpleNamespace(id=row.id, pcs_slug=slug)
                   for row in page
                   if (slug := request_rider_slug(row.pcs_slug, row.first_name, row.last_name))]


def enqueue_rider_check(job, now, enqueue_group, namespace, params, *, job_id=None, executed_by_id=None):
    """Stage requests for the rider job's predefined selection."""
    db.session.execute(text('SELECT pg_advisory_xact_lock(812734906)'),
                       bind_arguments={'mapper': ExternalCallTaskGroup})
    pcs_request_key = func.coalesce(ExternalCallTask.request_url, ExternalCallTask.request_params_json)
    if job == IDENTITY_CHECK:
        limit = params.get('riders_per_run', 100)
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError('Choose between 1 and 1000 riders per run.')
        utc_now = now.astimezone(timezone.utc).replace(tzinfo=None)
        days = params.get('min_days_since_check', 0)
        active_or_recent = ExternalCallTask.status.in_(ACTIVE_TASKS)
        if days:
            active_or_recent = or_(active_or_recent, ExternalCallTask.finished_at >= utc_now - timedelta(days=days))
        selected = []
        for page in _identity_candidates(namespace):
            keys_by_id = {rider.id: _pcs_request_keys(rider.pcs_slug) for rider in page}
            pending = db.session.execute(select(
                ExternalCallTask.related_to_id, pcs_request_key,
            ).join(
                ExternalCallTaskGroup, ExternalCallTaskGroup.id == ExternalCallTask.task_group_id,
            ).where(
                ExternalCallTaskGroup.namespace_id == namespace.id,
                ExternalCallTask.provider == 'pcs',
                ExternalCallTask.endpoint == PROFILE_ENDPOINT,
                active_or_recent,
                or_(
                    (ExternalCallTask.related_to_table == 'rider')
                    & ExternalCallTask.related_to_id.in_(keys_by_id),
                    ExternalCallTask.related_to_table.is_(None)
                    & pcs_request_key.in_([key for keys in keys_by_id.values() for key in keys]),
                ),
            )).all()
            excluded_ids = {rider_id for rider_id, _ in pending if rider_id is not None}
            excluded_keys = {key for rider_id, key in pending if rider_id is None}
            for rider in page:
                if (rider.id not in excluded_ids
                        and excluded_keys.isdisjoint(keys_by_id[rider.id])):
                    selected.append(rider)
                    if len(selected) == limit:
                        break
            if len(selected) == limit:
                break
        if not selected:
            return []
        origin = 'scheduled' if executed_by_id is None else 'manual'
        group = enqueue_group([
            dict(provider='pcs', endpoint=PROFILE_ENDPOINT, params={'rider_slug': rider.pcs_slug},
                 related_to_table='rider', related_to_id=rider.id)
            for rider in selected
        ], namespace_id=namespace.id, origin=origin, provider='pcs', executed_by_id=executed_by_id,
            workflow_type=job, message=RIDER_CHECK_JOBS[job], scheduled_job_id=job_id)
        return [group.id]

    rider = scheduled_rider(namespace, params['rider_id'])
    if rider is None:
        raise ValueError('The selected rider needs valid PCS and FirstCycling links.')
    rider_slug = request_rider_slug(rider.pcs_slug, rider.first_name, rider.last_name)
    if rider_slug is None:
        raise ValueError('The selected rider needs a valid PCS slug or name.')

    utc_now = now.astimezone(timezone.utc).replace(tzinfo=None)
    active_or_recent = ExternalCallTask.status.in_(ACTIVE_TASKS)
    days = params.get('min_days_since_check', 0)
    if days:
        active_or_recent = or_(active_or_recent, ExternalCallTask.finished_at >= utc_now - timedelta(days=days))
    pending = db.session.scalar(select(ExternalCallTask.id).join(
        ExternalCallTaskGroup, ExternalCallTaskGroup.id == ExternalCallTask.task_group_id,
    ).where(
        ExternalCallTaskGroup.namespace_id == namespace.id, ExternalCallTask.endpoint == PROFILE_ENDPOINT,
        or_(
            (ExternalCallTask.related_to_table == 'rider') & (ExternalCallTask.related_to_id == rider.id),
            ExternalCallTask.related_to_table.is_(None) & (ExternalCallTask.provider == 'pcs')
            & pcs_request_key.in_(_pcs_request_keys(rider_slug)),
        ),
        active_or_recent,
    ).limit(1))
    if pending:
        return []
    if (job == ACTIVITY_CHECK
            and _recent_fc_riders([rider.first_cycling_id], now.date(), params.get('recent_activity_days', 0))):
        return []

    origin = 'scheduled' if executed_by_id is None else 'manual'
    requests = [
        dict(provider=provider, endpoint=PROFILE_ENDPOINT, params=request_params,
             related_to_table='rider', related_to_id=rider.id)
        for provider, request_params in (
            ('pcs', {'rider_slug': rider_slug}),
            ('firstcycling', {'rider_id': rider.first_cycling_id}),
        )
    ]
    group = enqueue_group(
        requests, namespace_id=namespace.id, origin=origin, executed_by_id=executed_by_id,
        workflow_type=job, message=RIDER_CHECK_JOBS[job], scheduled_job_id=job_id,
    )
    return [group.id]


def _date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _store_pcs_profile(slug, payload, checked_at):
    profile = payload['profile']
    birth_date = _date(profile.get('birthdate'))
    latest = payload.get('latest_participation') or {}
    values = dict(profile_url=rider_profile_url(slug),
                  display_name=profile.get('display_name') or None, birth_date=birth_date,
                  nationality=profile.get('nationality') or None, source_payload=profile,
                  last_synced_at=checked_at)
    if latest:
        values.update(
            latest_participation_year=latest.get('year'),
            latest_participation_date=_date(latest.get('date')),
            latest_participation_race_name=latest.get('race_name'),
            latest_participation_race_category=latest.get('race_category'),
            latest_participation_race_type=latest.get('race_type'),
            latest_participation_race_id=latest.get('race_id'),
            latest_participation_race_url=latest.get('race_url'),
            latest_participation_stage_number=latest.get('stage_number'),
            latest_participation_stage_name=latest.get('stage_name'),
            latest_participation_row_type=latest.get('row_type'),
            latest_participation_result_position=latest.get('result'),
            latest_participation_status=latest.get('participation_status'),
            latest_participation_confirmed=latest.get('confirmed_participation'),
        )
    statement = insert(PcsRider).values(rider_slug=slug, **values)
    rider_id = db.session.scalar(statement.on_conflict_do_update(
        index_elements=[PcsRider.rider_slug], set_=values,
    ).returning(PcsRider.id))
    occurrences = Counter()
    results = []
    for row in payload['rows']:
        identity = (row['race_url'], row['date'], row['row_type'])
        occurrences[identity] += 1
        key = sha256(repr((*identity, occurrences[identity])).encode()).hexdigest()
        values = dict(
            season=row['year'], result_date=_date(row['date']), race_id=row['race_id'],
            race_url=row['race_url'], race_name=row['race_name'], race_category=row['race_category'],
            race_type=row['race_type'], stage_number=row['stage_number'], stage_name=row['stage_name'],
            row_type=row['row_type'], result_position=row['result'],
            participation_status=row['participation_status'],
            confirmed_participation=row['confirmed_participation'], source_payload=row,
            last_synced_at=checked_at,
        )
        results.append(dict(rider_id=rider_id, source_result_key=key, **values))
    if results:
        statement = insert(PcsRiderResult)
        db.session.execute(statement.on_conflict_do_update(
            constraint='uq_pcs_rider_result_key',
            set_={key: statement.excluded[key] for key in values},
        ), results)
    return birth_date


def apply_rider_check(task, params, payload, status, checked_at):
    """Persist provider facts and update the linked rider if it still uses this slug."""
    if task.endpoint != PROFILE_ENDPOINT:
        return
    if task.provider == 'firstcycling':
        if status == 'success':
            rider_id = int(params['rider_id'])
            FirstCyclingClient()._upsert_rider(rider_id, payload, f'rider/{rider_id}.json')
        return
    if task.provider != 'pcs':
        return
    slug = rider_slug_from_profile_url(task.request_url)
    if not slug:
        return
    birth_date = _store_pcs_profile(slug, payload, checked_at) if status == 'success' else None
    namespace = task.task_group.namespace if task.task_group else None
    linked = task.related_to_table == 'rider' and task.related_to_id is not None
    if not linked and (task.related_to_table is not None or namespace is None or not namespace.is_active):
        return
    with rider_database(namespace) as conn:
        riders = select(RIDER.c.id, RIDER.c.first_name, RIDER.c.last_name, RIDER.c.pcs_slug,
                        RIDER.c.birth_date, RIDER.c.pcs_latest_updated_at).where(
            RIDER.c.is_deleted.is_(False),
        )
        if linked:
            riders = riders.where(RIDER.c.id == task.related_to_id)
        else:
            riders = riders.where(RIDER.c.pcs_slug == slug)
        matches = conn.execute(riders.limit(2).with_for_update()).all()
        if len(matches) != 1:
            return
        current = matches[0]
        if request_rider_slug(current.pcs_slug, current.first_name, current.last_name) != slug:
            return
        if current.pcs_latest_updated_at and current.pcs_latest_updated_at >= checked_at:
            return
        task.related_to_table, task.related_to_id = 'rider', current.id
        updates = dict(pcs_identity_status='failed', pcs_latest_updated_at=checked_at)
        if status == 'success':
            updates.update(
                pcs_birth_date=birth_date,
                pcs_identity_status=pcs_dob_identity_status(current.birth_date, birth_date),
            )
        if (current.pcs_slug or '').strip() in ('', '0'):
            updates['pcs_slug'] = slug
        conn.execute(RIDER.update().where(RIDER.c.id == current.id).values(**updates))
