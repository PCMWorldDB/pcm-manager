"""Exercise scheduled selection and completion against isolated namespaces."""
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy_utils import create_database, drop_database

from webapp import worker
from webapp.web import routes
from webapp.extensions import db
from webapp.integrations.rider_checks import ACTIVITY_CHECK, IDENTITY_CHECK
from webapp.integrations.cdb import attribute_changes, field_configuration
from webapp.models import (
    Action, ExternalCallTask, ExternalCallTaskGroup, ScheduledJob, Namespace,
    PcsRider, PcsRiderResult, Resource, Role, RolePermission, User,
)
from webapp.models.main.firstcycling import FirstCyclingRace, FirstCyclingRaceResult, FirstCyclingRider
from webapp.models.ns.cdb import Rider
from webapp.models.ns.base import NamespaceBase


NOW = datetime(2026, 5, 20, 12, tzinfo=timezone.utc)


@pytest.fixture
def namespaces(app, monkeypatch):
    base = make_url(os.environ['WORLDDB_TEST_DATABASE_URL'])
    resources = []
    with app.app_context():
        try:
            for suffix in ('a', 'b'):
                name = 'worlddb_test_' + uuid4().hex
                url = base.set(database=name)
                create_database(url)
                engine = create_engine(url)
                namespace = Namespace(name=name, slug=name, database_name=name)
                db.session.add(namespace)
                resources.append((namespace, engine, suffix))
                NamespaceBase.metadata.create_all(engine)
                with engine.begin() as conn:
                    conn.execute(Rider.__table__.insert(), [
                        dict(id=i, pcs_slug=f'{suffix}-rider-{i}', birth_date=20000101,
                             first_cycling_id=990000 + i)
                        for i in range(1, 8)
                    ])
            db.session.commit()
            monkeypatch.setattr(Namespace, 'build_connection_string',
                                lambda self: base.set(database=self.database_name))
            yield [(namespace, engine) for namespace, engine, _ in resources]
        finally:
            db.session.rollback()
            ids = [namespace.id for namespace, _, _ in resources]
            group_ids = select(ExternalCallTaskGroup.id).where(ExternalCallTaskGroup.namespace_id.in_(ids))
            db.session.query(ExternalCallTask).filter(ExternalCallTask.task_group_id.in_(group_ids)).delete()
            db.session.query(ExternalCallTaskGroup).filter(ExternalCallTaskGroup.namespace_id.in_(ids)).delete()
            db.session.query(ScheduledJob).filter(ScheduledJob.namespace_id.in_(ids)).delete()
            db.session.query(FirstCyclingRace).filter_by(race_id=999991).delete()
            db.session.query(FirstCyclingRider).filter(FirstCyclingRider.rider_id.between(990001, 990007)).delete()
            db.session.query(PcsRider).filter(PcsRider.rider_slug.in_(['a-rider-1', 'b-rider-1'])).delete()
            db.session.query(Namespace).filter(Namespace.id.in_(ids)).delete()
            db.session.commit()
            for _, engine, _ in resources:
                engine.dispose()
                drop_database(engine.url)


def enqueue(namespace, rider_id, provider='pcs', status='queued', days_ago=0):
    params = {'rider_slug': f'a-rider-{rider_id}'} if provider == 'pcs' else {'rider_id': 990000 + rider_id}
    group = worker.enqueue_group([
        dict(provider=provider, endpoint='get_rider_profile', params=params,
             related_to_table='rider', related_to_id=rider_id),
    ], namespace_id=namespace.id)
    task = ExternalCallTask.query.filter_by(task_group_id=group.id).one()
    task.status = status
    if status in {'success', 'failed'}:
        task.finished_at = NOW.replace(tzinfo=None) - timedelta(days=days_ago)
    db.session.commit()
    return task


def test_scheduled_identity_selects_bounded_unknown_riders(namespaces, monkeypatch):
    (first, first_engine), (second, _) = namespaces
    with first_engine.begin() as conn:
        conn.execute(text("UPDATE rider SET pcs_identity_status='verified' WHERE id=1"))
        conn.execute(text("UPDATE rider SET pcs_identity_verified_override=TRUE WHERE id=2"))
        conn.execute(text("UPDATE rider SET first_cycling_id=NULL, pcs_slug='0', "
                          "first_name='A Rider', last_name='3' WHERE id=3"))
    jobs = [
        ScheduledJob(name='Identity', job_type=IDENTITY_CHECK, namespace_id=first.id,
                     enabled=True, run_at=time(8), params={'riders_per_run': 2}),
        ScheduledJob(name='Activity', job_type=ACTIVITY_CHECK, namespace_id=second.id,
                     enabled=True, run_at=time(8), params={'rider_id': 3, 'recent_activity_days': 7}),
    ]
    db.session.add_all(jobs)
    db.session.commit()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz)

    monkeypatch.setattr(worker, 'datetime', Clock)
    worker.poll_external_sync_schedules.run()
    worker.poll_external_sync_schedules.run()
    rows = db.session.query(ExternalCallTaskGroup, ExternalCallTask).join(
        ExternalCallTask, ExternalCallTask.task_group_id == ExternalCallTaskGroup.id,
    ).filter(
        ExternalCallTaskGroup.scheduled_job_id.in_([job.id for job in jobs]),
    ).order_by(ExternalCallTaskGroup.id).all()
    assert [(group.scheduled_job_id, group.namespace_id, task.provider, task.related_to_id) for group, task in rows] == [
        (jobs[0].id, first.id, 'pcs', 3), (jobs[0].id, first.id, 'pcs', 4),
        (jobs[1].id, second.id, 'pcs', 3), (jobs[1].id, second.id, 'firstcycling', 3),
    ]
    assert rows[0][1].request_url == 'https://www.procyclingstats.com/rider/a-rider-3/road'
    assert rows[0][0].id == rows[1][0].id
    assert rows[0][0].requested_count == 2
    assert rows[2][1].request_url == 'https://www.procyclingstats.com/rider/b-rider-3/road'
    assert rows[2][0].id == rows[3][0].id
    assert rows[2][0].workflow_type == ACTIVITY_CHECK
    assert rows[2][0].sync_types == ['firstcycling.get_rider_profile', 'pcs.get_rider_profile']
    assert all(job.last_run_on == NOW.date() for job in jobs)


def test_identity_batch_skips_active_and_recent_pcs_checks(namespaces):
    (first, engine), _ = namespaces
    active = enqueue(first, 1, status='queued')
    recent = enqueue(first, 2, status='success', days_ago=1)
    for task in (active, recent):
        task.related_to_id = task.related_to_table = None
    recent.request_url = None
    recent.request_params_json = '{"rider_slug": "a-rider-2"}'
    db.session.commit()
    enqueue(first, 3, provider='firstcycling', status='queued')
    with engine.begin() as conn:
        conn.execute(text("UPDATE rider SET pcs_identity_status='verified' WHERE id=4"))
    ids = worker.enqueue_rider_check(
        IDENTITY_CHECK, NOW, worker.enqueue_group, first,
        {'riders_per_run': 2, 'min_days_since_check': 3},
    )
    db.session.commit()
    assert len(ids) == 1
    tasks = ExternalCallTask.query.filter_by(task_group_id=ids[0]).order_by(ExternalCallTask.id).all()
    assert [(task.provider, task.related_to_id) for task in tasks] == [('pcs', 3), ('pcs', 5)]


def test_scheduled_activity_respects_recent_race_results(namespaces):
    (first, _), _ = namespaces
    race = FirstCyclingRace(race_id=999991, season_year=2026, end_date=NOW.date() - timedelta(days=2))
    db.session.add(FirstCyclingRaceResult(race=race, rider_id=990001))
    job = ScheduledJob(name='Activity', job_type=ACTIVITY_CHECK, namespace_id=first.id,
                       enabled=True, run_at=time(8), params={'rider_id': 1, 'recent_activity_days': 7})
    db.session.add(job)
    db.session.commit()
    worker._queue_due_scheduled_job(job.id, NOW)
    assert not ExternalCallTaskGroup.query.filter_by(scheduled_job_id=job.id).count()
    assert job.last_run_on == NOW.date()


def test_run_now_uses_only_selected_rider_and_preserves_job(namespaces, monkeypatch):
    (first, _), (second, _) = namespaces
    user = User(username=f'run-now-{uuid4().hex}', is_active=True)
    job = ScheduledJob(name='Identity', job_type=IDENTITY_CHECK, namespace_id=second.id,
                       enabled=False, run_at=time(8), params={'riders_per_run': 2}, last_run_on=NOW.date())
    db.session.add_all([user, job])
    db.session.commit()
    submitted = []
    monkeypatch.setattr(worker, 'submit_external_group', submitted.append)
    try:
        ids = worker.run_rider_check_now.run(IDENTITY_CHECK, second.id, user.id, {'riders_per_run': 3}, job_id=job.id)
        assert len(ids) == 1 and ids == submitted
        for group_id in ids:
            group = db.session.get(ExternalCallTaskGroup, group_id)
            assert (group.namespace_id, group.origin, group.executed_by_id) == (second.id, 'manual', user.id)
            assert group.scheduled_job_id == job.id
            assert [task.related_to_id for task in ExternalCallTask.query.filter_by(task_group_id=group.id).order_by(ExternalCallTask.id)] == [1, 2, 3]
        assert not ExternalCallTaskGroup.query.filter_by(namespace_id=first.id).count()
        assert job.last_run_on == NOW.date() and not job.enabled and job.params == {'riders_per_run': 2}
    finally:
        db.session.delete(user)
        db.session.commit()


def test_rider_job_form_checks_namespace_and_links(namespaces, client, monkeypatch):
    (first, first_engine), (second, second_engine) = namespaces
    with second_engine.begin() as conn:
        conn.execute(Rider.__table__.update().where(Rider.id == 2).values(first_cycling_id=None))
    with first_engine.begin() as conn:
        conn.execute(Rider.__table__.insert().values(id=8, pcs_slug='only-in-first', first_cycling_id=999999))
    user = User(username=f'schedule-manager-{uuid4().hex}', is_active=True)
    user.roles.append(Role.query.filter_by(name='integrations_manager').one())
    db.session.add(user)
    db.session.commit()
    try:
        with client.session_transaction() as session:
            session['_user_id'] = str(user.id)
        path = f'/n/{first.slug}/admin/integrations'
        page = client.get(f'{path}?tab=scheduler&new=1&job_type={IDENTITY_CHECK}&namespace_id={second.id}')
        assert page.status_code == 200
        assert 'name="rider_id"' not in page.get_data(as_text=True)
        assert 'name="riders_per_run"' in page.get_data(as_text=True)
        form = dict(job_type=IDENTITY_CHECK, name='Rider B', run_at='08:00', namespace_id=second.id, riders_per_run=2)
        assert client.post(f'{path}/scheduler', data={**form, 'riders_per_run': 0}).status_code == 400
        assert client.post(f'{path}/scheduler', data={**form, 'riders_per_run': 1001}).status_code == 400
        assert client.post(f'{path}/scheduler', data={**form, 'namespace_id': ''}).status_code == 400
        assert client.post(f'{path}/scheduler', data={**form, 'rider_id': 8}).status_code == 302
        job = ScheduledJob.query.filter_by(name='Rider B').one()
        assert job.namespace_id == second.id and job.params == {'riders_per_run': 2, 'min_days_since_check': 0} and not job.enabled
        assert client.post(f'{path}/scheduler', data={**form, 'job_id': job.id, 'namespace_id': first.id}).status_code == 400

        sent = []
        monkeypatch.setattr(worker.run_rider_check_now, 'delay', lambda *args, **kwargs: sent.append((args, kwargs)))
        assert client.post(f'{path}/scheduler', data={**form, 'job_id': job.id, 'riders_per_run': 3,
                                                     'action': 'run_now'}).status_code == 302
        assert sent == [((IDENTITY_CHECK, second.id, user.id, {'riders_per_run': 3, 'min_days_since_check': 0}),
                         {'job_id': job.id})]
        db.session.refresh(job)
        assert job.params['riders_per_run'] == 2
    finally:
        db.session.delete(user)
        db.session.commit()


@pytest.mark.parametrize('decision, expected_dob, expected_identity', [
    ('approve', 20010101, 'review'), ('reject', 20000101, 'verified'),
])
def test_pcs_check_uses_applied_dob_while_attribute_proposal_is_pending(
        namespaces, monkeypatch, decision, expected_dob, expected_identity):
    class Clock(datetime):
        offset = timedelta()

        @classmethod
        def utcnow(cls):
            return datetime.utcnow() + cls.offset

    monkeypatch.setattr(worker, 'datetime', Clock)
    (namespace, first), (_, second) = namespaces
    with first.begin() as conn:
        field_configuration.seed_fields(conn, 'PCM2026')
        policy = field_configuration.stored_fields(conn, 'rider')[('rider', 'birth_date')]
        assert policy['is_require_approval']
        change_id = attribute_changes.propose(conn, 'rider', 1, 'birth_date', '2001-01-01', None)
        assert conn.scalar(text('SELECT birth_date FROM rider WHERE id=1')) == 20000101
        assert conn.execute(text('SELECT status,applied_at FROM cdb_changes WHERE id=:id'),
                            {'id': change_id}).one() == ('pending', None)
    monkeypatch.setattr(worker, 'fetch_profile', lambda *args: {
        'profile': {'birthdate': '2000-01-01', 'display_name': 'A Rider'}, 'rows': [],
    })
    task = enqueue(namespace, 1)
    assert worker.execute_task(task.id)
    with first.begin() as conn:
        assert conn.execute(text('SELECT birth_date,pcs_identity_status FROM rider WHERE id=1')).one() == (
            20000101, 'verified',
        )
        assert conn.execute(text('SELECT status,applied_at FROM cdb_changes WHERE id=:id'),
                            {'id': change_id}).one() == ('pending', None)
        attribute_changes.decide(conn, 'rider', change_id, decision, None)
        assert conn.scalar(text('SELECT birth_date FROM rider WHERE id=1')) == expected_dob
    # The second synthetic fetch occurs after the queue's mandatory PCS spacing.
    Clock.offset = timedelta(seconds=10)
    task = enqueue(namespace, 1)
    assert worker.execute_task(task.id)
    with first.connect() as conn:
        assert conn.execute(text('SELECT birth_date,pcs_identity_status FROM rider WHERE id=1')).one() == (
            expected_dob, expected_identity,
        )
    with second.connect() as conn:
        assert conn.execute(text('SELECT birth_date,pcs_identity_status FROM rider WHERE id=1')).one() == (
            20000101, 'unknown',
        )


@pytest.mark.parametrize('pcs_dob, expected_status, stored_dob', [
    ('2000-01-01', 'verified', date(2000, 1, 1)), ('2001-01-01', 'review', date(2001, 1, 1)),
    ('', 'review', None), (None, 'failed', date(2000, 2, 2)),
])
def test_pcs_completion_updates_only_linked_rider_and_persists_facts(
        namespaces, monkeypatch, pcs_dob, expected_status, stored_dob):
    (namespace, first), (_, second) = namespaces
    task = enqueue(namespace, 1)
    with first.begin() as conn:
        conn.execute(text("""
            UPDATE rider SET pcs_identity_verified_override=TRUE, pcs_birth_date='2000-02-02' WHERE id=1
        """))

    def fetch(*args):
        if pcs_dob is None:
            raise ValueError('Rider not found')
        payload = {
            'profile': {'birthdate': pcs_dob, 'display_name': 'A Rider'},
            'rows': [{
                'year': 2026, 'date': '2026-05-10', 'race_id': 'race-2026',
                'race_url': 'https://www.procyclingstats.com/race/example/2026/result',
                'race_name': 'Race', 'race_category': '1.1', 'race_type': 'UCI', 'stage_number': None,
                'stage_name': '', 'row_type': 'one_day_result', 'result': '2',
                'participation_status': 'confirmed_participation', 'confirmed_participation': True,
            }],
        }
        payload['latest_participation'] = payload['rows'][0]
        return payload

    monkeypatch.setattr(worker, 'fetch_profile', fetch)
    assert worker.execute_task(task.id)
    assert task.status == ('failed' if pcs_dob is None else 'success')
    with first.connect() as conn:
        row = conn.execute(text('SELECT * FROM rider WHERE id=1')).one()
        assert (row.pcs_identity_status, row.pcs_birth_date) == (expected_status, stored_dob)
        assert row.pcs_identity_verified_override and row.pcs_latest_updated_at == task.finished_at
    with second.connect() as conn:
        assert conn.execute(text('SELECT pcs_identity_status, pcs_birth_date FROM rider WHERE id=1')).one() == ('unknown', None)
    assert PcsRider.query.filter_by(rider_slug='a-rider-1').count() == (pcs_dob is not None)
    assert PcsRiderResult.query.join(PcsRider).filter(PcsRider.rider_slug == 'a-rider-1').count() == (pcs_dob is not None)
    if pcs_dob is not None:
        profile = PcsRider.query.filter_by(rider_slug='a-rider-1').one()
        assert profile.latest_participation_date == date(2026, 5, 10)
        assert profile.latest_participation_race_name == 'Race'
        assert profile.latest_participation_result_position == '2'


def test_completion_preserves_relinked_rider_and_saves_fc_profile(app, namespaces, monkeypatch):
    (namespace, engine), _ = namespaces
    pcs = enqueue(namespace, 1)
    fc = enqueue(namespace, 1, provider='firstcycling')
    with engine.begin() as conn:
        conn.execute(text("UPDATE rider SET pcs_slug='replacement-slug' WHERE id=1"))
    monkeypatch.setitem(app.config, 'FIRST_CYCLING_API_PATH', 'synthetic')
    monkeypatch.setattr(worker, 'fetch_profile', lambda provider, params:
                        {'profile': {'birthdate': '2000-01-01'}, 'rows': []} if provider == 'pcs'
                        else {'description': {'firstName': 'A', 'lastName': 'Rider', 'birthDate': '2000-01-01'}})
    assert worker.execute_task(pcs.id) and worker.execute_task(fc.id)
    assert pcs.status == fc.status == 'success'
    with engine.connect() as conn:
        assert conn.execute(text('SELECT pcs_latest_updated_at FROM rider WHERE id=1')).scalar_one() is None
    assert FirstCyclingRider.query.filter_by(rider_id=990001).one().name == 'A Rider'


def test_pcs_completion_rejects_mismatched_namespace_database(namespaces, monkeypatch):
    (namespace, first), (_, second) = namespaces
    task = enqueue(namespace, 1)
    monkeypatch.setattr(Namespace, 'build_connection_string', lambda self: second.url)
    monkeypatch.setattr(worker, 'fetch_profile', lambda *args: {'profile': {'birthdate': '2000-01-01'}, 'rows': []})
    assert worker.execute_task(task.id)
    assert task.status == 'failed' and task.finished_at is not None
    assert task.http_status == 200
    assert PcsRider.query.filter_by(rider_slug='a-rider-1').count() == 0
    for engine in (first, second):
        with engine.connect() as conn:
            assert conn.execute(text('SELECT pcs_latest_updated_at FROM rider WHERE id=1')).scalar_one() is None


def test_manual_pcs_completion_links_the_unique_slug_in_its_namespace(namespaces, monkeypatch):
    (namespace, first), (_, second) = namespaces
    group = worker.enqueue_group([dict(provider='pcs', endpoint='get_rider_profile',
                                       params={'rider_slug': 'a-rider-1'})], namespace_id=namespace.id)
    db.session.commit()
    task = ExternalCallTask.query.filter_by(task_group_id=group.id).one()
    monkeypatch.setattr(worker, 'fetch_profile', lambda *args: {'profile': {'birthdate': '2000-01-01'}, 'rows': []})
    assert worker.execute_task(task.id)
    assert (task.status, task.related_to_table, task.related_to_id) == ('success', 'rider', 1)
    with first.connect() as conn:
        assert conn.execute(text('SELECT pcs_identity_status FROM rider WHERE id=1')).scalar_one() == 'verified'
    with second.connect() as conn:
        assert conn.execute(text('SELECT pcs_identity_status FROM rider WHERE id=1')).scalar_one() == 'unknown'


def test_rider_bulk_tasks_permissions_history_and_namespace(app, namespaces, client, monkeypatch):
    (first, first_engine), (second, second_engine) = namespaces
    path = f'/n/{second.slug}/riders/bulk-tasks'
    read_role = Role(name=uuid4().hex, display_name='Rider task reader')
    run_role = Role(name=uuid4().hex, display_name='Rider task runner')
    db.session.add_all([read_role, run_role])
    for role, resource, action in (
        (read_role, 'teams', 'read'),
        (read_role, 'stats', 'read'),
        (read_role, 'integrations-external-calls', 'read'),
        (run_role, 'integrations-external-calls', 'download'),
        (run_role, 'integrations-firstcycling', 'download'),
    ):
        resource_row = Resource.query.filter_by(name=resource).one()
        action_row = Action.query.filter_by(name=action).one()
        role.permissions.append(RolePermission(resource=resource_row, action=action_row))
    viewer_role = Role.query.filter_by(name='viewer').one()
    reader = User(username=uuid4().hex, is_active=True, roles=[read_role])
    runner = User(username=uuid4().hex, is_active=True, roles=[read_role, run_role])
    viewer = User(username=uuid4().hex, is_active=True, roles=[viewer_role])
    pcs = ExternalCallTaskGroup(namespace=second, provider='pcs', sync_types=['pcs.get_rider_profile'], status='running')
    fc = ExternalCallTaskGroup(namespace=second, provider='firstcycling', sync_types=['firstcycling.get_rider_profile'], status='success')
    other = ExternalCallTaskGroup(namespace=first, provider='pcs', sync_types=['pcs.get_rider_profile'])
    unrelated = ExternalCallTaskGroup(namespace=second, provider='firstcycling', workflow_type='fc_race_results')
    global_pcs = ExternalCallTaskGroup(provider='pcs', sync_types=['pcs.get_rider_profile'])
    db.session.add_all([reader, runner, viewer, pcs, fc, other, unrelated, global_pcs])
    db.session.commit()
    with second_engine.begin() as conn:
        conn.execute(text('UPDATE rider SET first_cycling_id=991002 WHERE id=2'))
        conn.execute(text('UPDATE rider SET is_deleted=TRUE WHERE id=7'))
    submitted = []
    monkeypatch.setattr(worker, 'submit_external_group', submitted.append)
    monkeypatch.setattr(routes, 'submit_external_group', submitted.append)

    def request(url=path, **kwargs):
        with app.app_context():
            return client.open(url, **kwargs)

    def login(user):
        with client.session_transaction() as session:
            session['_user_id'] = str(user.id)
            session['_fresh'] = True

    def create_and_queue(data):
        response = request(method='POST', data=data)
        assert response.status_code == 302
        group_id = int(parse_qs(urlsplit(response.location).query)['group_id'][0])
        group = db.session.get(ExternalCallTaskGroup, group_id)
        assert group.status == 'draft'
        assert request(method='POST', data={'action': 'queue', 'group_id': group_id}).status_code == 302
        return group

    try:
        form = {'sync_types': ['pcs.get_rider_profile'], 'rider_ids': '1, 2\n1'}
        assert request().status_code == 302
        login(viewer)
        assert request().status_code == 403
        assert request(method='POST', data=form).status_code == 403
        login(reader)
        response = request()
        assert response.status_code == 200 and response.cache_control.no_store
        markup = BeautifulSoup(response.data, 'html.parser')
        tabs = markup.select('nav[aria-label="Riders sections"] a')
        assert [tab.get_text(strip=True) for tab in tabs][-2:] == ['Bulk Editor', 'Bulk Tasks']
        assert tabs[-1]['aria-current'] == 'page'
        assert markup.select_one('#bulk-task-type') is None
        links = markup.select('tbody th a')
        assert {link.get_text(strip=True) for link in links} == {f'#{pcs.id}', f'#{fc.id}', f'#{unrelated.id}'}
        filtered = BeautifulSoup(request(f'{path}?sync_type=pcs.get_rider_profile').data, 'html.parser')
        assert [link.get_text(strip=True) for link in filtered.select('tbody th a')] == [f'#{pcs.id}']
        assert request(links[0]['href']).status_code == 200
        assert request(method='POST', data=form).status_code == 403
        assert request(method='POST', data={**form, 'workflow_type': IDENTITY_CHECK}).status_code == 403

        login(runner)
        assert request('/n/missing/riders/bulk-tasks').status_code == 404
        assert request(method='POST', data={**form, 'sync_types': ['fc_race_results']}).status_code == 400
        assert request(method='POST', data={**form, 'rider_ids': '1,7'}).status_code == 302
        activity_form = {**form, 'workflow_type': ACTIVITY_CHECK}
        assert request(method='POST', data=activity_form).status_code == 403
        with monkeypatch.context() as patch:
            patch.setattr(Namespace, 'build_connection_string', lambda self: first_engine.url)
            assert request(method='POST', data=form).status_code == 302
        assert not submitted

        create_and_queue(form)
        create_and_queue({**form, 'sync_types': ['firstcycling.get_rider_profile'], 'rider_ids': '2'})
        assert len(submitted) == 2
        groups = [db.session.get(ExternalCallTaskGroup, group_id) for group_id in submitted]
        assert [(group.namespace_id, group.executed_by_id, group.sync_types, group.requested_count)
                for group in groups] == [(second.id, runner.id, ['pcs.get_rider_profile'], 2), (second.id, runner.id, ['firstcycling.get_rider_profile'], 1)]
        tasks = ExternalCallTask.query.filter_by(task_group_id=groups[0].id).order_by(ExternalCallTask.id).all()
        assert [(task.related_to_table, task.related_to_id, task.request_url.rsplit('/', 2)[1]) for task in tasks] == [
            ('rider', 1, 'b-rider-1'), ('rider', 2, 'b-rider-2'),
        ]
        task = ExternalCallTask.query.filter_by(task_group_id=groups[1].id).one()
        assert json.loads(task.request_params_json) == {'rider_id': 991002}

        create_and_queue({**form, 'workflow_type': IDENTITY_CHECK})
        identity = db.session.get(ExternalCallTaskGroup, submitted[-1])
        assert (identity.namespace_id, identity.workflow_type, identity.requested_count) == (
            second.id, IDENTITY_CHECK, 2,
        )

        read_role.permissions.append(RolePermission(
            resource=Resource.query.filter_by(name='activity-monitor').one(),
            action=Action.query.filter_by(name='read').one(),
        ))
        second.database_year = 2026
        db.session.commit()
        riders_path = f'/n/{second.slug}/riders'
        request(riders_path, method='POST', data={'action': 'set_view', 'view': 'activity'})
        markup = BeautifulSoup(request(riders_path).data, 'html.parser')
        assert markup.select_one('.dropdown-menu button[value="run_data_sync"]')['formaction'] == path
        assert not markup.select_one('button[value="rider_activity_check"]')
        assert not markup.select_one('button[value="rider_identity_check"]')
        with second_engine.begin() as conn:
            conn.execute(text('UPDATE rider SET first_cycling_id=NULL WHERE id=1'))
        assert request(method='POST', data=activity_form).status_code == 302
        assert len(submitted) == 3
        with second_engine.begin() as conn:
            conn.execute(text('UPDATE rider SET first_cycling_id=991001 WHERE id=1'))
        create_and_queue(activity_form)
        assert len(submitted) == 4
        activity = db.session.get(ExternalCallTaskGroup, submitted[-1])
        assert (activity.namespace_id, activity.workflow_type, activity.requested_count) == (
            second.id, ACTIVITY_CHECK, 4,
        )
        tasks = ExternalCallTask.query.filter_by(task_group_id=activity.id).all()
        assert {(task.provider, task.related_to_id) for task in tasks} == {
            ('pcs', 1), ('firstcycling', 1), ('pcs', 2), ('firstcycling', 2),
        }
    finally:
        ExternalCallTaskGroup.query.filter_by(id=global_pcs.id).delete()
        for user in (viewer, reader, runner):
            db.session.delete(user)
        db.session.delete(read_role)
        db.session.delete(run_role)
        db.session.commit()


def test_rider_page_queues_only_one_pcs_task_in_selected_namespace(app, namespaces, client, monkeypatch):
    (first, _), (second, engine) = namespaces
    profile_url = f'/n/{second.slug}/riders/1'
    check_url = f'{profile_url}/check-identity'
    with engine.begin() as conn:
        conn.execute(text("UPDATE rider SET first_cycling_id=NULL, pcs_slug='0', "
                          "first_name='B Rider', last_name='1' WHERE id=1"))
        conn.execute(text("UPDATE rider SET pcs_slug=NULL WHERE id=2"))
        conn.execute(text("UPDATE rider SET is_deleted=TRUE WHERE id=3"))
    viewer_role = Role.query.filter_by(name='viewer').one()
    admin_role = Role.query.filter_by(name='admin').one()
    viewer = User(username=uuid4().hex, is_active=True, roles=[viewer_role])
    admin = User(username=uuid4().hex, is_active=True, roles=[admin_role])
    db.session.add_all([viewer, admin])
    db.session.commit()
    submitted = []
    monkeypatch.setattr(worker, 'submit_external_group', submitted.append)

    def request(path, **kwargs):
        with app.app_context():
            return client.open(path, **kwargs)

    try:
        assert request(check_url, method='POST').status_code == 302
        with client.session_transaction() as session:
            session['_user_id'] = str(viewer.id)
            session['_fresh'] = True
        page = request(profile_url)
        assert page.status_code == 200
        assert not BeautifulSoup(page.data, 'html.parser').select('#rider-identity button[aria-label="Check PCS identity"]')
        assert request(check_url, method='POST').status_code == 403

        with client.session_transaction() as session:
            session['_user_id'] = str(admin.id)
            session['_fresh'] = True
        page = request(profile_url)
        assert page.status_code == 200
        markup = BeautifulSoup(page.data, 'html.parser')
        check_button = markup.select_one('#rider-identity-title + form button[aria-label="Check PCS identity"]')
        assert check_button and check_button.get_text(strip=True) == 'Check'
        assert 'btn-success' in check_button.get('class', [])
        assert check_button.find_parent('form')['action'] == check_url
        assert not BeautifulSoup(request(f'/n/{second.slug}/riders/2').data, 'html.parser').select(
            '#rider-identity button[aria-label="Check PCS identity"]')
        assert request(f'/n/{second.slug}/riders/2/check-identity', method='POST').status_code == 302
        assert request(f'/n/{second.slug}/riders/3/check-identity', method='POST').status_code == 404
        assert request('/n/missing/riders/1/check-identity', method='POST').status_code == 404
        assert not submitted

        response = request(check_url, method='POST', data={'rider_slug': 'a-rider-1', 'namespace_id': first.id})
        assert response.location == profile_url
        group = ExternalCallTaskGroup.query.filter_by(executed_by_id=admin.id).one()
        assert submitted == [group.id]
        assert (group.namespace_id, group.provider, group.sync_types, group.origin, group.requested_count) == (
            second.id, 'pcs', ['pcs.get_rider_profile'], 'manual', 1,
        )
        task = ExternalCallTask.query.filter_by(task_group_id=group.id).one()
        assert (task.provider, task.endpoint, task.related_to_table, task.related_to_id) == (
            'pcs', 'get_rider_profile', 'rider', 1,
        )
        assert task.request_url == 'https://www.procyclingstats.com/rider/b-rider-1/road'
        assert json.loads(task.request_params_json) == {'rider_slug': 'b-rider-1'}
        monkeypatch.setattr(worker, 'fetch_profile', lambda *args: {
            'profile': {'birthdate': '2000-01-01'}, 'rows': [],
        })
        assert worker.execute_task(task.id)
        with engine.connect() as conn:
            assert conn.execute(text('SELECT pcs_slug FROM rider WHERE id=1')).scalar_one() == 'b-rider-1'
    finally:
        db.session.delete(viewer)
        db.session.delete(admin)
        db.session.commit()


@pytest.mark.parametrize('denied_provider', ['pcs', 'firstcycling'])
def test_mixed_group_requires_every_method_permission(app, namespaces, client, monkeypatch, denied_provider):
    (_, _), (namespace, _) = namespaces
    user = User(username=uuid4().hex, is_active=True, roles=[
        Role.query.filter_by(name='viewer').one(),
        Role.query.filter_by(name='integrations_manager').one(),
    ])
    db.session.add(user)
    db.session.flush()
    group = worker.enqueue_group([
        dict(provider=provider, endpoint='get_rider_profile', params=params,
             related_to_table='rider', related_to_id=1)
        for provider, params in [('pcs', {'rider_slug': 'b-rider-1'}),
                                 ('firstcycling', {'rider_id': 991001})]
    ], namespace_id=namespace.id, executed_by_id=user.id, draft=True)
    db.session.commit()
    group_id = group.id
    path = f'/n/{namespace.slug}/riders/bulk-tasks'
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    permission = '_can_check_rider_identity' if denied_provider == 'pcs' else '_can_run_firstcycling_integrations'
    monkeypatch.setattr(routes, permission, lambda: False)
    allowed_type = 'firstcycling.get_rider_profile' if denied_provider == 'pcs' else 'pcs.get_rider_profile'
    monkeypatch.setattr(routes, 'submit_external_group', lambda *args: pytest.fail('Denied group was submitted'))
    try:
        with app.app_context():
            page = BeautifulSoup(client.get(f'{path}?tab=editor&group_id={group_id}').data, 'html.parser')
            assert not page.select('button[value="queue"], button[value="cancel"], #task-group-form')
            for action in ('save', 'add', 'remove', 'queue', 'cancel'):
                response = client.post(path, data={
                    'action': action, 'group_id': group_id, 'sync_types': [allowed_type],
                    'rider_ids': '2', 'rider_id': 1,
                })
                assert response.status_code == 403
            assert client.post(path, data={
                'action': 'create', 'sync_types': ['pcs.get_rider_profile', 'firstcycling.get_rider_profile'],
                'rider_ids': '1',
            }).status_code == 403
        db.session.expire_all()
        assert group.status == 'draft' and group.requested_count == 2
        assert group.sync_types == ['firstcycling.get_rider_profile', 'pcs.get_rider_profile']
        assert ExternalCallTask.query.filter_by(task_group_id=group_id).count() == 2
    finally:
        db.session.delete(user)
        db.session.commit()


def test_bulk_task_draft_editor_and_queue_are_namespace_scoped(app, namespaces, client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    (first, _), (namespace, engine) = namespaces
    path = f'/n/{namespace.slug}/riders/bulk-tasks'
    user = User(username=uuid4().hex, is_active=True, roles=[
        Role.query.filter_by(name='viewer').one(),
        Role.query.filter_by(name='integrations_manager').one(),
    ])
    db.session.add(user)
    db.session.commit()
    user_id = user.id
    submitted = []
    monkeypatch.setattr(routes, 'submit_external_group', submitted.append)
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True

    def request(url=path, **kwargs):
        with app.app_context():
            response = client.open(url, **kwargs)
        db.session.expire_all()
        return response

    def post(action, **data):
        return request(method='POST', data={'action': action, 'group_id': group_id, **data})

    try:
        response = request(method='POST', data={'action': 'create', 'sync_types': ['pcs.get_rider_profile'], 'name': 'My draft'})
        group_id = int(parse_qs(urlsplit(response.location).query)['group_id'][0])
        group = db.session.get(ExternalCallTaskGroup, group_id)
        assert (group.status, group.requested_count, group.message) == ('draft', 0, 'My draft')
        markup = BeautifulSoup(request(response.location).data, 'html.parser')
        assert [tab.get_text(strip=True) for tab in markup.select('nav[aria-label="Bulk Tasks sections"] a')] == ['Groups', 'Editor']
        assert {option['value'] for option in markup.select('#bulk-task-type option')} == {'pcs.get_rider_profile', 'firstcycling.get_rider_profile'}
        assert markup.select_one('button[value="queue"][disabled]')
        assert post('queue').status_code == 302 and group.status == 'draft'
        assert not submitted

        other_path = f'/n/{first.slug}/riders/bulk-tasks'
        assert request(f'{other_path}?tab=editor&group_id={group_id}').status_code == 404
        assert request(other_path, method='POST', data={'action': 'add', 'group_id': group_id, 'rider_ids': '1'}).status_code == 404
        assert post('add', rider_ids='1,2,1').status_code == 302
        assert post('add', rider_ids='2').status_code == 302
        tasks = ExternalCallTask.query.filter_by(task_group_id=group_id).order_by(ExternalCallTask.id).all()
        assert [(task.related_to_id, task.status) for task in tasks] == [(1, 'draft'), (2, 'draft')]
        assert not worker.execute_task(tasks[0].id)
        assert not worker.submit_external_group(group_id)
        assert not submitted

        # A method change rebuilds every request from this namespace's riders.
        with engine.begin() as conn:
            conn.execute(text('UPDATE rider SET first_cycling_id=NULL WHERE id=2'))
        assert post('save', sync_types=['firstcycling.get_rider_profile']).status_code == 302
        assert group.sync_types == ['pcs.get_rider_profile']
        assert {task.provider for task in ExternalCallTask.query.filter_by(task_group_id=group_id)} == {'pcs'}
        with engine.begin() as conn:
            conn.execute(text('UPDATE rider SET first_cycling_id=991002 WHERE id=2'))
        assert post('save', sync_types=['firstcycling.get_rider_profile'], name='FC draft').status_code == 302
        assert (group.sync_types, group.message) == (['firstcycling.get_rider_profile'], 'FC draft')
        tasks = ExternalCallTask.query.filter_by(task_group_id=group_id).all()
        assert {task.provider for task in tasks} == {'firstcycling'}
        assert {json.loads(task.request_params_json)['rider_id'] for task in tasks} == {990001, 991002}
        assert post('remove', rider_id=1).status_code == 302
        assert group.requested_count == 1
        assert post('add', rider_ids='1').status_code == 302
        assert post('save', sync_types=['pcs.get_rider_profile', 'firstcycling.get_rider_profile']).status_code == 302
        assert group.sync_types == ['firstcycling.get_rider_profile', 'pcs.get_rider_profile']
        assert group.workflow_type is None and group.requested_count == 4
        for kind in group.sync_types:
            filtered = BeautifulSoup(request(f'{path}?sync_type={kind}').data, 'html.parser')
            assert f'#{group_id}' in [link.get_text(strip=True) for link in filtered.select('tbody th a')]

        def queue_once(_):
            with app.test_client() as concurrent_client:
                with concurrent_client.session_transaction() as session:
                    session['_user_id'] = str(user_id)
                    session['_fresh'] = True
                return concurrent_client.post(path, data={'action': 'queue', 'group_id': group_id}).status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sorted(executor.map(queue_once, range(2))) == [302, 409]
        db.session.expire_all()
        assert submitted == [group_id] and group.status == 'queued'
        for action in ('add', 'remove', 'save', 'queue'):
            assert post(action, rider_ids='3', rider_id=1).status_code == 409
        markup = BeautifulSoup(request(f'{path}?tab=editor&group_id={group_id}').data, 'html.parser')
        assert markup.select_one('button[value="cancel"]')
        assert not markup.select('button[value="save"], button[value="add"], button[value="remove"], button[value="queue"]')
        assert post('cancel').status_code == 302
        assert group.status == 'cancelled'
        assert {task.status for task in ExternalCallTask.query.filter_by(task_group_id=group_id)} == {'cancelled'}
        assert post('add', rider_ids='3').status_code == 409
    finally:
        db.session.rollback()
        db.session.delete(db.session.get(User, user_id))
        db.session.commit()
