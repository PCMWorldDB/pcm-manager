"""Verify the unified external integration views and manual sync entry point."""
import json
from datetime import datetime, time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from webapp import worker
from webapp.extensions import db
from webapp.models import ExternalCallTask, ExternalCallTaskGroup, ScheduledJob, Namespace, Role, User
from webapp.web import routes


@pytest.fixture
def integration_data(app):
    with app.app_context():
        suffix = uuid4().hex
        manager = User(username=f'fc-manager-{suffix}', is_active=True)
        manager.roles.append(Role.query.filter_by(name='integrations_manager').one())
        viewer = User(username=f'fc-viewer-{suffix}', is_active=True)
        selected = Namespace(name=f'Selected {suffix}', slug=f'selected-{suffix}', database_name=f'selected_{suffix}')
        other = Namespace(name=f'Other {suffix}', slug=f'other-{suffix}', database_name=f'other_{suffix}')
        firstcycling = ExternalCallTaskGroup(
            provider='firstcycling', sync_type='fc_race_results', status='partial',
            season_year=2026, gender='Men', executed_by=manager, message='Race results sync complete',
            requested_count=2, synced_count=1, failed_count=1,
            details_json=json.dumps({'start_date': '2026-05-01', 'end_date': '2026-05-31', 'rows_synced': 30}),
        )
        shared = ExternalCallTaskGroup(namespace=selected, origin='scheduled', priority='low',
                                       status='running', message='Selected namespace group')
        hidden = ExternalCallTaskGroup(namespace=other, message='Other namespace group')
        empty = ExternalCallTaskGroup(provider='firstcycling', status='queued', details_json='invalid legacy JSON')
        groups = [firstcycling, shared, hidden, empty]
        db.session.add_all([manager, viewer, *groups])
        db.session.add_all([
            ExternalCallTask(task_group=firstcycling, provider='firstcycling', endpoint='2026/42.json',
                             attempt=1, max_attempts=2, http_status=429, outcome='http_retry'),
            ExternalCallTask(task_group=firstcycling, provider='firstcycling', endpoint='2026/42.json',
                             attempt=2, max_attempts=2, http_status=200, outcome='success', duration_ms=0),
            ExternalCallTask(task_group=shared, provider='pcs', endpoint='get_rider_profile', status='retry_wait'),
            ExternalCallTask(task_group=hidden, provider='pcs', endpoint='other-group-endpoint'),
        ])
        db.session.commit()
        manager_id, viewer_id = manager.id, viewer.id
        namespace_ids = [selected.id, other.id]
        group_ids = [group.id for group in groups]
        path = f'/n/{selected.slug}/admin/integrations'

    yield manager_id, viewer_id, group_ids, path

    with app.app_context():
        ExternalCallTask.query.filter(ExternalCallTask.task_group_id.in_(group_ids)).delete()
        ExternalCallTaskGroup.query.filter(ExternalCallTaskGroup.id.in_(group_ids)).delete()
        Namespace.query.filter(Namespace.id.in_(namespace_ids)).delete()
        db.session.delete(db.session.get(User, manager_id))
        db.session.delete(db.session.get(User, viewer_id))
        db.session.commit()


def test_task_groups_filters_and_detail(app, client, integration_data, monkeypatch):
    manager_id, viewer_id, (fc_id, shared_id, hidden_id, empty_id), path = integration_data
    detail = f'{path}/task-groups'
    assert client.get(f'{detail}/{fc_id}').status_code == 302
    with client.session_transaction() as session:
        session['_user_id'] = str(viewer_id)
    assert client.get(path).status_code == 403
    assert client.get(f'{detail}/{shared_id}').status_code == 403

    with client.session_transaction() as session:
        session['_user_id'] = str(manager_id)
    response = client.get(path)
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'External Data Integrations' in page
    assert 'Manual Syncs' in page
    assert f'href="{detail}/{fc_id}"' in page
    assert f'href="{detail}/{shared_id}"' in page
    assert 'Other namespace group' not in page

    filtered = client.get(f'{path}?provider=pcs&status=running&priority=low&origin=scheduled').get_data(as_text=True)
    assert 'Selected namespace group' in filtered
    assert 'Race results sync complete' not in filtered
    legacy = client.get(f'{path}/firstcycling?sync_view=fc_race_results&year=2026&gender=Men', follow_redirects=True)
    assert legacy.status_code == 200
    assert 'Race results sync complete' in legacy.get_data(as_text=True)
    assert 'Selected namespace group' not in legacy.get_data(as_text=True)

    response = client.get(f'{detail}/{fc_id}')
    assert response.status_code == 200
    assert response.cache_control.no_store
    page = response.get_data(as_text=True)
    assert f'External Call Task Group #{fc_id}' in page
    assert 'Partial' in page
    assert '2026-05-01 to 2026-05-31' in page
    assert 'Tasks (2)' in page
    assert page.count('2026/42.json') == 2
    assert 'Historical' in page
    assert 'Http retry' in page
    assert 'Success' in page
    assert '0 ms' in page
    assert 'other-group-endpoint' not in page
    assert 'Retry wait' in client.get(f'{detail}/{shared_id}').get_data(as_text=True)
    assert 'No task items have been recorded' in client.get(f'{detail}/{empty_id}').get_data(as_text=True)
    assert client.get(f'{detail}/{hidden_id}').status_code == 404
    with app.app_context():
        task = ExternalCallTask.query.filter_by(task_group_id=shared_id).one()
        task.request_url = 'https://www.procyclingstats.com/rider/jane-doe/road'
        task.result_json = '{"profile":{"display_name":"Jane Doe"}}'
        task.error_message = 'Temporary provider error'
        task.related_to_table = 'rider'
        task.related_to_id = 3365
        db.session.commit()
        task_id = task.id
    task_url = f'{detail}/{shared_id}/tasks/{task_id}'
    rider_lookups = []

    def load_rider(namespace, rider_id):
        rider_lookups.append((namespace.slug, rider_id))
        return SimpleNamespace(first_name='Jane', last_name='Doe', name='Stored Display Name')

    monkeypatch.setattr(routes, '_load_namespace_roster_rider', load_rider)
    response = client.get(task_url)
    assert response.status_code == 200 and response.cache_control.no_store
    assert 'Jane Doe' in response.get_data(as_text=True)
    assert 'Temporary provider error' in response.get_data(as_text=True)
    assert 'https://www.procyclingstats.com/rider/jane-doe/road' in response.get_data(as_text=True)
    rider_url = path.removesuffix('/admin/integrations') + '/riders/3365'
    assert 'rider #3365' in response.get_data(as_text=True)
    assert f'href="{rider_url}"' not in response.get_data(as_text=True)
    assert not rider_lookups
    assert f'href="{task_url}"' in client.get(f'{detail}/{shared_id}').get_data(as_text=True)
    assert client.get(f'{detail}/{hidden_id}/tasks/{task_id}').status_code == 404
    assert client.get(f'{detail}/{shared_id}/tasks/999999999').status_code == 404
    with client.session_transaction() as session:
        session['_user_id'] = str(viewer_id)
    assert client.get(task_url).status_code == 403
    with app.app_context():
        viewer = db.session.get(User, viewer_id)
        viewer.roles.append(Role.query.filter_by(name='admin').one())
        db.session.commit()
    response = client.get(task_url)
    assert response.status_code == 200
    assert f'href="{rider_url}">Jane Doe</a>' in response.get_data(as_text=True)
    assert rider_lookups == [(path.split('/')[2], 3365)]


def test_manual_tab_starts_existing_firstcycling_sync(app, client, integration_data, monkeypatch):
    manager_id, viewer_id, group_ids, path = integration_data
    monkeypatch.setitem(app.config, 'SEASONS', [{'year': 2026, 'gender': 'Men'}])
    monkeypatch.setitem(app.config, 'FIRST_CYCLING_API_PATH', 'synthetic-api-path')
    dispatched = []
    monkeypatch.setattr(routes.run_external_sync_task, 'delay', lambda **kwargs: dispatched.append(kwargs))
    form = {'provider': 'firstcycling', 'sync_type': 'fc_race_results', 'year': '2026', 'gender': 'Men',
            'start_date': '2026-05-01', 'end_date': '2026-05-31'}
    with client.session_transaction() as session:
        session['_user_id'] = str(viewer_id)
    assert client.post(f'{path}/run', data=form).status_code == 403

    with client.session_transaction() as session:
        session['_user_id'] = str(manager_id)
    page = client.get(f'{path}?tab=manual&sync_view=fc_race_results&year=2026&gender=Men')
    assert page.status_code == 200
    assert 'Start Sync - Race Results' in page.get_data(as_text=True)
    assert 'name="start_date"' in page.get_data(as_text=True)

    response = client.post(f'{path}/run', data=form)
    for call in dispatched:
        group_ids.append(call['log_id'])
    assert response.status_code == 302
    location = urlsplit(response.location)
    assert location.path == path
    assert parse_qs(location.query)['tab'] == ['manual']
    assert len(dispatched) == 1
    assert dispatched[0]['start_date'] == form['start_date']
    assert dispatched[0]['end_date'] == form['end_date']
    with app.app_context():
        group = db.session.get(ExternalCallTaskGroup, dispatched[0]['log_id'])
        assert (group.provider, group.sync_type, group.status) == ('firstcycling', 'fc_race_results', 'queued')
        assert group.namespace_id is None


def test_manual_sync_inputs_and_pcs_rider_queue(app, client, integration_data, monkeypatch):
    manager_id, viewer_id, group_ids, path = integration_data
    monkeypatch.setitem(app.config, 'SEASONS', [{'year': 2026, 'gender': 'Men'}])
    monkeypatch.setitem(app.config, 'FIRST_CYCLING_API_PATH', 'synthetic-api-path')
    submitted = []
    monkeypatch.setattr(worker, 'submit_external_group', submitted.append)
    dispatched = []
    monkeypatch.setattr(routes.run_external_sync_task, 'delay', lambda **kwargs: dispatched.append(kwargs))
    form = {'provider': 'pcs', 'sync_type': 'pcs_rider', 'rider_slug': 'jane-doe'}
    with client.session_transaction() as session:
        session['_user_id'] = str(viewer_id)
    assert client.post(f'{path}/run', data=form).status_code == 403
    with client.session_transaction() as session:
        session['_user_id'] = str(manager_id)

    for kind, names in (
        ('fc_race_calendar', {'year', 'gender'}),
        ('fc_teams', {'year', 'gender', 'limit'}),
        ('fc_riders', {'year', 'gender', 'limit'}),
        ('fc_race_results', {'year', 'gender', 'start_date', 'end_date'}),
        ('pcs_rider', {'rider_slug'}),
    ):
        response = client.get(f'{path}?tab=manual&sync_view={kind}')
        assert response.status_code == 200
        page = response.get_data(as_text=True)
        assert 'Select a Season' not in page
        for label in ('FC Race Calendar', 'FC Teams', 'FC Riders', 'FC Race Results', 'PCS Rider'):
            assert label in page
        for field in ('year', 'gender', 'limit', 'start_date', 'end_date', 'rider_slug'):
            assert (f'name="{field}"' in page) == (field in names)

    response = client.post(f'{path}/run', data={**form, 'rider_slug': '../jane-doe'})
    assert response.status_code == 302
    assert not submitted
    response = client.post(f'{path}/run', data=form)
    group_ids.extend(submitted)
    assert response.status_code == 302
    assert len(submitted) == 1
    with app.app_context():
        group = db.session.get(ExternalCallTaskGroup, submitted[0])
        assert group.namespace.slug == path.split('/')[2]
        task = ExternalCallTask.query.filter_by(task_group_id=group.id).one()
        assert (task.provider, task.endpoint, task.status) == ('pcs', 'get_rider_profile', 'queued')
        assert task.request_url == 'https://www.procyclingstats.com/rider/jane-doe/road'
        assert json.loads(task.request_params_json) == {'rider_slug': 'jane-doe'}

    teams = {'provider': 'firstcycling', 'sync_type': 'fc_teams', 'year': 2026, 'gender': 'Men', 'limit': 20}
    client.post(f'{path}/run', data={**teams, 'gender': 'Unsupported'})
    assert not dispatched
    response = client.post(f'{path}/run', data=teams)
    group_ids.extend(call['log_id'] for call in dispatched)
    assert response.status_code == 302
    assert len(dispatched) == 1
    assert dispatched[0]['limit'] == 20


def test_named_race_jobs_run_independently_and_start_empty(app, client, integration_data, monkeypatch):
    manager_id, viewer_id, group_ids, path = integration_data
    monkeypatch.setitem(app.config, 'SEASONS', [{'year': 2026, 'gender': 'Men'}])
    monkeypatch.setitem(app.config, 'FIRST_CYCLING_API_PATH', 'synthetic-api-path')
    form = dict(job_type='race_results', name='Morning races', enabled='1', run_at='08:00',
                year=2026, gender='Men', start_days_ago=1, end_days_ago=0)
    url = f'{path}/scheduler'
    with client.session_transaction() as session:
        session['_user_id'] = str(viewer_id)
    assert client.post(url, data=form).status_code == 403
    with client.session_transaction() as session:
        session['_user_id'] = str(manager_id)

    page = client.get(f'{path}?tab=scheduler').get_data(as_text=True)
    assert '>Job</a>' in page and '>Create New Job</a>' in page
    assert 'No scheduled jobs have been created' in page
    assert 'name="job_type"' not in page
    page = client.get(f'{path}?tab=scheduler&new=1').get_data(as_text=True)
    assert 'Rider Identity Check' in page and 'Rider Activity Check' in page
    assert 'Yesterday' in page and '>Run Now</button>' in page
    assert 'No scheduled jobs have been created' not in page
    assert client.post(url, data={**form, 'end_days_ago': 2}).status_code == 400
    assert client.post(url, data=form).status_code == 302
    assert client.post(url, data={**form, 'name': 'Yesterday only', 'end_days_ago': 1}).status_code == 302

    calls, manual = [], []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 5, 2, 8, 0, tzinfo=tz)

    class FakeFirstCyclingClient:
        def __init__(self, api_path=None, external_sync_log_id=None):
            group_ids.append(external_sync_log_id)

        def sync_race_results_window(self, **kwargs):
            calls.append(kwargs)
            return {'requested': 1, 'synced': 1, 'failed': 0}

    monkeypatch.setattr(worker, 'datetime', Clock)
    monkeypatch.setattr(routes, 'datetime', Clock)
    monkeypatch.setattr(worker, 'FirstCyclingClient', FakeFirstCyclingClient)
    monkeypatch.setattr(routes.run_external_sync_task, 'delay', lambda **kwargs: manual.append(kwargs))
    try:
        with app.app_context():
            jobs = ScheduledJob.query.order_by(ScheduledJob.id).all()
            assert len(jobs) == 2
            job_id = jobs[0].id
            assert jobs[0].run_at == time(8) and jobs[0].namespace_id is None
            assert jobs[0].params['start_days_ago'] == 1
            worker.poll_external_sync_schedules.run()
            assert worker.poll_external_sync_schedules.run() is None
            assert [(call['start_date'], call['end_date']) for call in calls] == [
                ('2026-05-01', '2026-05-02'), ('2026-05-01', '2026-05-01'),
            ]
            groups = ExternalCallTaskGroup.query.filter_by(origin='scheduled', sync_type='fc_race_results').all()
            assert {group.scheduled_job_id for group in groups} == {job.id for job in jobs}
        page = client.get(f'{path}?tab=scheduler').get_data(as_text=True)
        assert 'Morning races' in page and '>Edit</a>' in page
        assert 'name="job_type"' not in page
        page = client.get(f'{path}?tab=scheduler&job_id={job_id}').get_data(as_text=True)
        assert 'Edit Morning races' in page and 'value="Morning races"' in page
        page = client.get(f'{path}?tab=scheduler&new=1').get_data(as_text=True)
        assert 'name="job_type"' in page and 'Morning races' not in page
        assert client.post(url, data={**form, 'job_id': job_id, 'start_days_ago': 3, 'action': 'run_now'}).status_code == 302
        group_ids.extend(run['log_id'] for run in manual)
        assert manual[0]['start_date'] == '2026-04-29'
        with app.app_context():
            assert db.session.get(ScheduledJob, job_id).params['start_days_ago'] == 1
        assert client.post(url, data=dict(job_id=job_id, job_type='race_results', action='disable')).status_code == 302
        with app.app_context():
            assert not db.session.get(ScheduledJob, job_id).enabled
    finally:
        with app.app_context():
            ScheduledJob.query.filter_by(job_type='race_results').delete()
            db.session.commit()
